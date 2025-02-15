import datetime
import os
import time
import torch
from torch.utils.data import DataLoader

import torch.nn.functional as F

from torch.utils.tensorboard import SummaryWriter
import sys
from torch.cuda import amp
# import smodels_firing_num
import smodels
import argparse
from spikingjelly.clock_driven import functional
from spikingjelly.datasets import cifar10_dvs
import math
import numpy as np
import pandas as pd

_seed_ = 2020
import random

random.seed(2020)

torch.manual_seed(_seed_)  # use torch.manual_seed() to seed the RNG for all devices (both CPU and CUDA)
torch.cuda.manual_seed_all(_seed_)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

import numpy as np

np.random.seed(_seed_)


def get_parameter_number(net):
    total_num = sum(p.numel() for p in net.parameters())
    trainable_num = sum(p.numel() for p in net.parameters() if p.requires_grad)
    return {'Total': total_num, 'Trainable': trainable_num}


def split_to_train_test_set(train_ratio: float, origin_dataset: torch.utils.data.Dataset, num_classes: int,
                            random_split: bool = False):
    '''
    :param train_ratio: split the ratio of the origin dataset as the train set
    :type train_ratio: float
    :param origin_dataset: the origin dataset
    :type origin_dataset: torch.utils.data.Dataset
    :param num_classes: total classes number, e.g., ``10`` for the MNIST dataset
    :type num_classes: int
    :param random_split: If ``False``, the front ratio of samples in each classes will
            be included in train set, while the reset will be included in test set.
            If ``True``, this function will split samples in each classes randomly. The randomness is controlled by
            ``numpy.randon.seed``
    :type random_split: int
    :return: a tuple ``(train_set, test_set)``
    :rtype: tuple
    '''
    label_idx = []
    for i in range(num_classes):
        label_idx.append([])

    for i, item in enumerate(origin_dataset):
        y = item[1]
        if isinstance(y, np.ndarray) or isinstance(y, torch.Tensor):
            y = y.item()
        label_idx[y].append(i)
    train_idx = []
    test_idx = []
    if random_split:
        for i in range(num_classes):
            np.random.shuffle(label_idx[i])

    for i in range(num_classes):
        pos = math.ceil(label_idx[i].__len__() * train_ratio)
        train_idx.extend(label_idx[i][0: pos])
        test_idx.extend(label_idx[i][pos: label_idx[i].__len__()])

    return torch.utils.data.Subset(origin_dataset, train_idx), torch.utils.data.Subset(origin_dataset, test_idx)


def main():
    parser = argparse.ArgumentParser(description='Classify DVS128 Gesture')
    parser.add_argument('-T', default=16, type=int, help='simulating time-steps')
    # parser.add_argument('-device', default='cuda:0', help='device')
    parser.add_argument('-device', default='cpu', help='device')
    parser.add_argument('-b', default=1, type=int, help='batch size')
    parser.add_argument('-epochs', default=64, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-j', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('-data_dir', type=str, default='D:/1/dataset')
    parser.add_argument('-out_dir', type=str, help='root dir for saving logs and checkpoint', default='./logs')

    parser.add_argument('-resume', type=str, help='resume from the checkpoint path',
                        default='D:/Github/Spike-Element-Wise-ResNet/origin_logs/cifar10dvs/checkpoint_max.pth')
    parser.add_argument('-amp', action='store_true', help='automatic mixed precision training')

    parser.add_argument('-opt', type=str, help='use which optimizer. SDG or Adam', default='SGD')
    parser.add_argument('-lr', default=0.1, type=float, help='learning rate')
    parser.add_argument('-momentum', default=0.9, type=float, help='momentum for SGD')
    parser.add_argument('-lr_scheduler', default='CosALR', type=str, help='use which schedule. StepLR or CosALR')
    parser.add_argument('-step_size', default=32, type=float, help='step_size for StepLR')
    parser.add_argument('-gamma', default=0.1, type=float, help='gamma for StepLR')
    parser.add_argument('-T_max', default=64, type=int, help='T_max for CosineAnnealingLR')
    parser.add_argument('-model', default='SEWResNet', type=str)
    parser.add_argument('-cnf', default='ADD', type=str)
    parser.add_argument('-T_train', default=None, type=int)
    parser.add_argument('-dts_cache', type=str, default='./dts_cache')

    args = parser.parse_args()
    print(args)

    train_set_pth = os.path.join(args.dts_cache, f'train_set_{args.T}.pt')
    test_set_pth = os.path.join(args.dts_cache, f'test_set_{args.T}.pt')
    if os.path.exists(train_set_pth) and os.path.exists(test_set_pth):
        train_set = torch.load(train_set_pth)
        test_set = torch.load(test_set_pth)
    else:
        origin_set = cifar10_dvs.CIFAR10DVS(root=args.data_dir, data_type='frame', frames_number=args.T,
                                            split_by='number')

        train_set, test_set = split_to_train_test_set(0.9, origin_set, 10)
        if not os.path.exists(args.dts_cache):
            os.makedirs(args.dts_cache)
        torch.save(train_set, train_set_pth)
        torch.save(test_set, test_set_pth)

    train_data_loader = DataLoader(
        dataset=train_set,
        batch_size=args.b,
        shuffle=True,
        num_workers=args.j,
        drop_last=True,
        pin_memory=True)

    test_data_loader = DataLoader(
        dataset=test_set,
        batch_size=args.b,
        shuffle=False,
        num_workers=args.j,
        drop_last=False,
        pin_memory=True)

    scaler = None
    if args.amp:
        scaler = amp.GradScaler()

    start_epoch = 0
    max_test_acc = 0

    net = smodels.__dict__[args.model](args.cnf)
    print(net)
    print(get_parameter_number(net))
    net.to(args.device)

    optimizer = None
    if args.opt == 'SGD':
        optimizer = torch.optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum)
    elif args.opt == 'Adam':
        optimizer = torch.optim.Adam(net.parameters(), lr=args.lr)
    else:
        raise NotImplementedError(args.opt)

    lr_scheduler = None
    if args.lr_scheduler == 'StepLR':
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    elif args.lr_scheduler == 'CosALR':
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.T_max)
    else:
        raise NotImplementedError(args.lr_scheduler)

    if args.resume:

        checkpoint = torch.load(args.resume, map_location='cpu')
        state_dict = checkpoint['net']
        keys1 = list(state_dict.keys())
        keys2 = list(net.state_dict().keys())
        for idx in range(len(keys1)):
            state_dict[keys2[idx]] = state_dict.pop(keys1[idx])
        net.load_state_dict(state_dict)

        optimizer.load_state_dict(checkpoint['optimizer'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        start_epoch = checkpoint['epoch'] + 1
        max_test_acc = checkpoint['max_test_acc']

    out_dir = os.path.join(args.out_dir,
                           f'{args.model}_{args.cnf}_T_{args.T}_T_train_{args.T_train}_{args.opt}_lr_{args.lr}_')
    if args.lr_scheduler == 'CosALR':
        out_dir += f'CosALR_{args.T_max}'
    elif args.lr_scheduler == 'StepLR':
        out_dir += f'StepLR_{args.step_size}_{args.gamma}'
    else:
        raise NotImplementedError(args.lr_scheduler)

    if args.amp:
        out_dir += '_amp'

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        print(f'Mkdir {out_dir}.')
    else:
        print(out_dir)
        assert args.resume is not None

    pt_dir = out_dir + '_pt'
    if not os.path.exists(pt_dir):
        os.makedirs(pt_dir)
        print(f'Mkdir {pt_dir}.')

    with open(os.path.join(out_dir, 'args.txt'), 'w', encoding='utf-8') as args_txt:
        args_txt.write(str(args))

    SummaryWriter(os.path.join(out_dir, 'logs'), purge_step=start_epoch)

    net.eval()
    test_loss = 0
    test_acc = 0
    test_samples = 0

    all_idx = 0
    save_path = './firing'

    with torch.no_grad():
        for frame, label in test_data_loader:
            frame = frame.float().to(args.device)
            label = label.to(args.device)
            out_fr, firing_num  = net(frame)

            lists = []
            for firing_single in firing_num:
                sub_list = []
                firing_single = firing_single.cpu().detach().numpy()
                for T_ in range(args.T):
                    sub_list.append(np.sum(firing_single[T_, :, :, :, :]))
                sub_list.append(firing_single[0, :, :, :, :].shape[0] * firing_single[0, :, :, :, :].shape[1] *
                                firing_single[0, :, :, :, :].shape[2] * firing_single[0, :, :, :, :].shape[3])
                lists.append(sub_list)
            csv = pd.DataFrame(
                data=lists
            )
            if not os.path.exists(save_path):
                os.makedirs(save_path)
            csv.to_csv(save_path + os.sep + str(all_idx) + '.csv')
            all_idx += 1


            loss = F.cross_entropy(out_fr, label)

            test_samples += label.numel()
            test_loss += loss.item() * label.numel()
            test_acc += (out_fr.argmax(1) == label).float().sum().item()
            functional.reset_net(net)

    test_loss /= test_samples
    test_acc /= test_samples
    print('test_acc', test_acc)

    # for epoch in range(start_epoch, args.epochs):
    #     start_time = time.time()
    #     net.train()
    #     train_loss = 0
    #     train_acc = 0
    #     train_samples = 0
    #     for frame, label in train_data_loader:
    #         optimizer.zero_grad()
    #         frame = frame.float().to(args.device)
    #
    #         if args.T_train:
    #             sec_list = np.random.choice(frame.shape[1], args.T_train, replace=False)
    #             sec_list.sort()
    #             frame = frame[:, sec_list]
    #
    #         label = label.to(args.device)
    #         if args.amp:
    #             with amp.autocast():
    #                 out_fr = net(frame)
    #                 loss = F.cross_entropy(out_fr, label)
    #             scaler.scale(loss).backward()
    #             scaler.step(optimizer)
    #             scaler.update()
    #         else:
    #             out_fr = net(frame)
    #             loss = F.cross_entropy(out_fr, label)
    #             loss.backward()
    #             optimizer.step()
    #
    #         train_samples += label.numel()
    #         train_loss += loss.item() * label.numel()
    #         train_acc += (out_fr.argmax(1) == label).float().sum().item()
    #
    #         functional.reset_net(net)
    #     train_loss /= train_samples
    #     train_acc /= train_samples
    #
    #     writer.add_scalar('train_loss', train_loss, epoch)
    #     writer.add_scalar('train_acc', train_acc, epoch)
    #     lr_scheduler.step()
    #
    #     net.eval()
    #     test_loss = 0
    #     test_acc = 0
    #     test_samples = 0
    #     with torch.no_grad():
    #         for frame, label in test_data_loader:
    #             frame = frame.float().to(args.device)
    #             label = label.to(args.device)
    #             out_fr = net(frame)
    #             loss = F.cross_entropy(out_fr, label)
    #
    #             test_samples += label.numel()
    #             test_loss += loss.item() * label.numel()
    #             test_acc += (out_fr.argmax(1) == label).float().sum().item()
    #             functional.reset_net(net)
    #
    #     test_loss /= test_samples
    #     test_acc /= test_samples
    #     writer.add_scalar('test_loss', test_loss, epoch)
    #     writer.add_scalar('test_acc', test_acc, epoch)
    #
    #     save_max = False
    #     if test_acc > max_test_acc:
    #         max_test_acc = test_acc
    #         save_max = True
    #
    #     checkpoint = {
    #         'net': net.state_dict(),
    #         'optimizer': optimizer.state_dict(),
    #         'lr_scheduler': lr_scheduler.state_dict(),
    #         'epoch': epoch,
    #         'max_test_acc': max_test_acc
    #     }
    #
    #     if save_max:
    #         torch.save(checkpoint, os.path.join(pt_dir, 'checkpoint_max.pth'))
    #
    #     torch.save(checkpoint, os.path.join(pt_dir, 'checkpoint_latest.pth'))
    #     for item in sys.argv:
    #         print(item, end=' ')
    #     print('')
    #     print(args)
    #     print(out_dir)
    #     total_time = time.time() - start_time
    #     print(
    #         f'epoch={epoch}, train_loss={train_loss}, train_acc={train_acc}, test_loss={test_loss}, test_acc={test_acc}, max_test_acc={max_test_acc}, total_time={total_time}, escape_time={(datetime.datetime.now() + datetime.timedelta(seconds=total_time * (args.epochs - epoch))).strftime("%Y-%m-%d %H:%M:%S")}')


if __name__ == '__main__':
    main()
