""" Training script for steps_with_decay policy"""

import argparse
import os
import sys
import pickle
import resource
import traceback
import logging
from collections import defaultdict

import numpy as np
import yaml
import torch
from torch.autograd import Variable
import torch.nn as nn
import cv2
cv2.setNumThreads(0)  # pytorch issue 1355: possible deadlock in dataloader

import _init_paths  # pylint: disable=unused-import
import nn as mynn
import utils.net as net_utils
import utils.misc as misc_utils
from core.config import cfg, cfg_from_file, cfg_from_list, assert_and_infer_cfg
from datasets.roidb import combined_roidb_for_training
from roi_data.loader import RoiDataLoader, MinibatchSampler, collate_minibatch
from modeling.model_builder import Generalized_RCNN
from utils.detectron_weight_helper import load_detectron_weight
from utils.logging import setup_logging
from utils.timer import Timer
from utils.training_stats import TrainingStats

# Set up logging and load config options
logger = setup_logging(__name__)
logging.getLogger('roi_data.loader').setLevel(logging.INFO)

# RuntimeError: received 0 items of ancdata. Issue: pytorch/pytorch#973
rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (4096, rlimit[1]))

def parse_args():
    """Parse input arguments"""
    parser = argparse.ArgumentParser(description='Train a X-RCNN network')

    parser.add_argument(
        '--dataset', dest='dataset',
        help='Dataset to use')
    parser.add_argument(
        '--suffix', dest='suffix',
        help='suffix of output dir')
    parser.add_argument(
        '--cfg', dest='cfg_file', required=True,
        help='Config file for training (and optionally testing)')
    parser.add_argument(
        '--set', dest='set_cfgs',
        help='Set config keys. Key value sequence seperate by whitespace.'
             'e.g. [key] [value] [key] [value]',
        default=[], nargs='+')

    parser.add_argument(
        '--disp_interval',
        help='Display training info every N iterations',
        default=20, type=int)
    parser.add_argument(
        '--no_cuda', dest='cuda', help='Do not use CUDA device', action='store_false')

    # Optimization
    # These options has the highest prioity and can overwrite the values in config file
    # or values set by set_cfgs. `None` means do not overwrite.
    parser.add_argument(
        '--bs', dest='batch_size',
        help='Explicitly specify to overwrite the value comed from cfg_file.',
        type=int)
    parser.add_argument(
        '--nw', dest='num_workers',
        help='Explicitly specify to overwrite number of workers to load data. Defaults to 4',
        type=int)

    parser.add_argument(
        '--o', dest='optimizer', help='Training optimizer.',
        default=None)
    parser.add_argument(
        '--lr', help='Base learning rate.',
        default=None, type=float)
    parser.add_argument(
        '--lr_decay_gamma',
        help='Learning rate decay rate.',
        default=None, type=float)

    # Epoch
    parser.add_argument(
        '--start_step',
        help='Starting step count for training epoch. 0-indexed.',
        default=0, type=int)

    # Resume training: requires same iterations per epoch
    parser.add_argument(
        '--resume',
        help='resume to training on a checkpoint',
        action='store_true')

    parser.add_argument(
        '--no_save', help='do not save anything', action='store_true')

    parser.add_argument(
        '--load_ckpt', help='checkpoint path to load')
    parser.add_argument(
        '--load_detectron', help='path to the detectron weight pickle file')

    parser.add_argument(
        '--use_tfboard', help='Use tensorflow tensorboard to log training info',
        action='store_true')

    return parser.parse_args()


def save_ckpt(output_dir, args, step, train_size, model, optimizer):
    """Save checkpoint"""
    if args.no_save:
        return
    ckpt_dir = os.path.join(output_dir, 'ckpt')
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    save_name = os.path.join(ckpt_dir, 'model_step{}.pth'.format(step))
    if isinstance(model, mynn.DataParallel):
        model = model.module
    model_state_dict = model.state_dict()
    torch.save({
        'step': step,
        'train_size': train_size,
        'batch_size': args.batch_size,
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict()}, save_name)
    logger.info('save model: %s', save_name)


def main():
    """Main function"""

    args = parse_args()
    print('Called with args:')
    print(args)

    if not torch.cuda.is_available():
        sys.exit("Need a CUDA device to run the code.")

    if args.cuda or cfg.NUM_GPUS > 0:
        cfg.CUDA = True
    else:
        raise ValueError("Need Cuda device to run !")

    if args.dataset == "coco2017":
        cfg.TRAIN.DATASETS = ('coco_2017_train',)
        cfg.MODEL.NUM_CLASSES = 81
    elif args.dataset == "keypoints_coco2017":
        cfg.TRAIN.DATASETS = ('keypoints_coco_2017_train',)
        cfg.MODEL.NUM_CLASSES = 2
    elif args.dataset == "coco2014_mini":
        cfg.TRAIN.DATASETS = ('coco_2014_minitrain',)
        cfg.MODEL.NUM_CLASSES = 81

    cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    ### Adaptively adjust some configs ###
    original_batch_size = cfg.NUM_GPUS * cfg.TRAIN.IMS_PER_BATCH
    if args.batch_size is None:
        args.batch_size = original_batch_size
    cfg.NUM_GPUS = torch.cuda.device_count()
    assert (args.batch_size % cfg.NUM_GPUS) == 0, \
        'batch_size: %d, NUM_GPUS: %d' % (args.batch_size, cfg.NUM_GPUS)
    cfg.TRAIN.IMS_PER_BATCH = args.batch_size // cfg.NUM_GPUS
    print('Batch size change from {} (in config file) to {}'.format(
        original_batch_size, args.batch_size))
    print('NUM_GPUs: %d, TRAIN.IMS_PER_BATCH: %d' % (cfg.NUM_GPUS, cfg.TRAIN.IMS_PER_BATCH))

    if args.num_workers is not None:
        cfg.DATA_LOADER.NUM_THREADS = args.num_workers
    print('Number of data loading threads: %d' % cfg.DATA_LOADER.NUM_THREADS)

    ### Adjust learning based on batch size change linearly
    old_base_lr = cfg.SOLVER.BASE_LR
    cfg.SOLVER.BASE_LR *= args.batch_size / original_batch_size
    print('Adjust BASE_LR linearly according to batch size change: {} --> {}'.format(
        old_base_lr, cfg.SOLVER.BASE_LR))

    ### Overwrite some solver settings from command line arguments
    if args.optimizer is not None:
        cfg.SOLVER.TYPE = args.optimizer
    if args.lr is not None:
        cfg.SOLVER.BASE_LR = args.lr
    if args.lr_decay_gamma is not None:
        cfg.SOLVER.GAMMA = args.lr_decay_gamma
    assert_and_infer_cfg()

    timers = defaultdict(Timer)

    ### Dataset ###
    timers['roidb'].tic()
    roidb, ratio_list, ratio_index = combined_roidb_for_training(
        cfg.TRAIN.DATASETS, cfg.TRAIN.PROPOSAL_FILES)
    timers['roidb'].toc()
    roidb_size = len(roidb)
    logger.info('{:d} roidb entries'.format(roidb_size))
    logger.info('Takes %.2f sec(s) to construct roidb', timers['roidb'].average_time)

    # Effective training sample size for one epoch
    train_size = roidb_size // args.batch_size * args.batch_size

    sampler = MinibatchSampler(ratio_list, ratio_index)
    dataset = RoiDataLoader(
        roidb,
        cfg.MODEL.NUM_CLASSES,
        training=True)
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        drop_last=True,
        sampler=sampler,
        num_workers=cfg.DATA_LOADER.NUM_THREADS,
        collate_fn=collate_minibatch)
    dataiterator = iter(dataloader)

    ### Model ###
    maskRCNN = Generalized_RCNN()

    if cfg.CUDA:
        maskRCNN.cuda()

    ### Optimizer ###
    bias_params = []
    nonbias_params = []
    for key, value in dict(maskRCNN.named_parameters()).items():
        if value.requires_grad:
            if 'bias' in key:
                bias_params.append(value)
            else:
                nonbias_params.append(value)
    # Learning rate of 0 is a dummy value to be set properly at the start of training
    params = [
        {'params': nonbias_params,
         'lr': 0,
         'weight_decay': cfg.SOLVER.WEIGHT_DECAY},
        {'params': bias_params,
         'lr': 0 * (cfg.SOLVER.BIAS_DOUBLE_LR + 1),
         'weight_decay': cfg.SOLVER.WEIGHT_DECAY if cfg.SOLVER.BIAS_WEIGHT_DECAY else 0}
    ]

    if cfg.SOLVER.TYPE == "SGD":
        optimizer = torch.optim.SGD(params, momentum=cfg.SOLVER.MOMENTUM)
    elif cfg.SOLVER.TYPE == "Adam":
        optimizer = torch.optim.Adam(params)

    ### Load checkpoint
    if args.load_ckpt:
        load_name = args.load_ckpt
        logging.info("loading checkpoint %s", load_name)
        checkpoint = torch.load(load_name, map_location=lambda storage, loc: storage)
        net_utils.load_ckpt(maskRCNN, checkpoint['model'])
        if args.resume:
            args.start_step = checkpoint['step'] + 1
            if 'train_size' in checkpoint:  # For backward compatibility
                assert checkpoint['train_size'] == train_size
            # There is a bug in optimizer.load_state_dict on Pytorch 0.3.1.
            # However it's fixed on master.
            # optimizer.load_state_dict(checkpoint['optimizer'])
            misc_utils.load_optimizer_state_dict(optimizer, checkpoint['optimizer'])
        del checkpoint
        torch.cuda.empty_cache()

    if args.load_detectron:  #TODO resume for detectron weights (load sgd momentum values)
        logging.info("loading Detectron weights %s", args.load_detectron)
        load_detectron_weight(maskRCNN, args.load_detectron)

    lr = optimizer.param_groups[0]['lr']  # lr of non-bias parameters, for commmand line outputs.

    maskRCNN = mynn.DataParallel(maskRCNN, cpu_keywords=['im_info', 'roidb'],
                                 minibatch=True)

    ### Training Setups ###
    args.run_name = misc_utils.get_run_name() + '_step'
    output_dir = misc_utils.get_output_dir(args, args.run_name)
    if args.suffix:
        output_dir = output_dir + '_' + args.suffix
    args.cfg_filename = os.path.basename(args.cfg_file)

    if not args.no_save:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        blob = {'cfg': yaml.dump(cfg), 'args': args}
        with open(os.path.join(output_dir, 'config_and_args.pkl'), 'wb') as f:
            pickle.dump(blob, f, pickle.HIGHEST_PROTOCOL)

        if args.use_tfboard:
            from tensorboardX import SummaryWriter
            # Set the Tensorboard logger
            tblogger = SummaryWriter(output_dir)

    ### Training Loop ###
    maskRCNN.train()

    CHECKPOINT_PERIOD = int(cfg.SOLVER.MAX_ITER / 2)

    # Set index for decay steps
    decay_steps_ind = None
    for i in range(1, len(cfg.SOLVER.STEPS)):
        if cfg.SOLVER.STEPS[i] >= args.start_step:
            decay_steps_ind = i
            break
    if decay_steps_ind is None:
        decay_steps_ind = len(cfg.SOLVER.STEPS)

    training_stats = TrainingStats(
        args,
        args.disp_interval,
        tblogger if args.use_tfboard and not args.no_save else None)
    try:
        logger.info('Training starts !')
        step = args.start_step
        for step in range(args.start_step, cfg.SOLVER.MAX_ITER):

            # Warm up
            if step < cfg.SOLVER.WARM_UP_ITERS:
                method = cfg.SOLVER.WARM_UP_METHOD
                if method == 'constant':
                    warmup_factor = cfg.SOLVER.WARM_UP_FACTOR
                elif method == 'linear':
                    alpha = step / cfg.SOLVER.WARM_UP_ITERS
                    warmup_factor = cfg.SOLVER.WARM_UP_FACTOR * (1 - alpha) + alpha
                else:
                    raise KeyError('Unknown SOLVER.WARM_UP_METHOD: {}'.format(method))
                lr_new = cfg.SOLVER.BASE_LR * warmup_factor
                net_utils.update_learning_rate(optimizer, lr, lr_new)
                lr = optimizer.param_groups[0]['lr']
                assert lr == lr_new
            elif step == cfg.SOLVER.WARM_UP_ITERS:
                net_utils.update_learning_rate(optimizer, lr, cfg.SOLVER.BASE_LR)
                lr = optimizer.param_groups[0]['lr']
                assert lr == cfg.SOLVER.BASE_LR

            # Learning rate decay
            if decay_steps_ind < len(cfg.SOLVER.STEPS) and \
                    step == cfg.SOLVER.STEPS[decay_steps_ind]:
                logger.info('Decay the learning on step %d', step)
                lr_new = lr * cfg.SOLVER.GAMMA
                net_utils.update_learning_rate(optimizer, lr, lr_new)
                lr = optimizer.param_groups[0]['lr']
                assert lr == lr_new
                decay_steps_ind += 1

            try:
                input_data = next(dataiterator)
            except StopIteration:
                dataiterator = iter(dataloader)
                input_data = next(dataiterator)

            if cfg.RPN.RPN_ON:
                for key in input_data:
                    if key != 'roidb': # roidb is a list of ndarrays with inconsistent length
                        input_data[key] = list(map(Variable, input_data[key]))
            else:
                input_data['data'] = list(map(Variable, input_data['data']))

            training_stats.IterTic()
            net_outputs = maskRCNN(**input_data)
            training_stats.IterToc()

            training_stats.UpdateIterStats(net_outputs)
            training_stats.LogIterStats(step, lr)

            loss = net_outputs['total_loss']
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if (step+1) % CHECKPOINT_PERIOD == 0:
                save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)

        # ---- Training ends ----
        # Save last checkpoint
        save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)

    except (RuntimeError, KeyboardInterrupt) as e:
        has_exception = True
        stack_trace = traceback.format_exc()
        print(stack_trace)

    finally:
        if locals().get('has_exception'):
            print('Save on exception')
            save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)

        if args.use_tfboard and not args.no_save:
            tblogger.close()


if __name__ == '__main__':
    main()
