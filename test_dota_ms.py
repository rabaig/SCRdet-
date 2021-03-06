# -*- coding:utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import os
import sys
import tensorflow as tf
import cv2
import numpy as np
import math
from tqdm import tqdm
import argparse
from multiprocessing import Queue, Process
sys.path.append("../")

from data.io.image_preprocess import short_side_resize_for_inference_data
from libs.networks import build_whole_network
from help_utils import tools
from libs.label_name_dict.label_dict import *
from libs.box_utils import draw_box_in_img
from libs.box_utils.coordinate_convert import forward_convert, backward_convert
from libs.box_utils import nms_rotate
from libs.box_utils.rotate_polygon_nms import rotate_gpu_nms


def worker(gpu_id, images, det_net, args, result_queue):
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    img_plac = tf.placeholder(dtype=tf.uint8, shape=[None, None, 3])  # is RGB. not BGR
    img_batch = tf.cast(img_plac, tf.float32)

    img_batch = short_side_resize_for_inference_data(img_tensor=img_batch,
                                                     target_shortside_len=cfgs.IMG_SHORT_SIDE_LEN,
                                                     length_limitation=cfgs.IMG_MAX_LENGTH,
                                                     is_resize=not args.multi_scale)
    if cfgs.NET_NAME in ['resnet152_v1d', 'resnet101_v1d', 'resnet50_v1d']:
        img_batch = (img_batch / 255 - tf.constant(cfgs.PIXEL_MEAN_)) / tf.constant(cfgs.PIXEL_STD)
    else:
        img_batch = img_batch - tf.constant(cfgs.PIXEL_MEAN)

    img_batch = tf.expand_dims(img_batch, axis=0)

    detection_boxes, detection_scores, detection_category = det_net.build_whole_detection_network(
        input_img_batch=img_batch,
        gtboxes_batch=None,
        gtboxes_r_batch=None,
        gpu_id=0)

    init_op = tf.group(
        tf.global_variables_initializer(),
        tf.local_variables_initializer()
    )

    restorer, restore_ckpt = det_net.get_restorer()

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True

    with tf.Session(config=config) as sess:
        sess.run(init_op)
        if not restorer is None:
            restorer.restore(sess, restore_ckpt)
            print('restore model %d ...' % gpu_id)

        for img_path in images:

            # if 'P0016' not in img_path:
            #     continue

            img = cv2.imread(img_path)

            box_res_rotate = []
            label_res_rotate = []
            score_res_rotate = []

            imgH = img.shape[0]
            imgW = img.shape[1]

            img_short_side_len_list = cfgs.IMG_SHORT_SIDE_LEN if args.multi_scale else [cfgs.IMG_SHORT_SIDE_LEN]

            if imgH < args.h_len:
                temp = np.zeros([args.h_len, imgW, 3], np.float32)
                temp[0:imgH, :, :] = img
                img = temp
                imgH = args.h_len

            if imgW < args.w_len:
                temp = np.zeros([imgH, args.w_len, 3], np.float32)
                temp[:, 0:imgW, :] = img
                img = temp
                imgW = args.w_len

            for hh in range(0, imgH, args.h_len - args.h_overlap):
                if imgH - hh - 1 < args.h_len:
                    hh_ = imgH - args.h_len
                else:
                    hh_ = hh
                for ww in range(0, imgW, args.w_len - args.w_overlap):
                    if imgW - ww - 1 < args.w_len:
                        ww_ = imgW - args.w_len
                    else:
                        ww_ = ww
                    src_img = img[hh_:(hh_ + args.h_len), ww_:(ww_ + args.w_len), :]

                    for short_size in img_short_side_len_list:
                        max_len = cfgs.IMG_MAX_LENGTH
                        if args.h_len < args.w_len:
                            new_h, new_w = short_size, min(int(short_size * float(args.w_len) / args.h_len), max_len)
                        else:
                            new_h, new_w = min(int(short_size * float(args.h_len) / args.w_len), max_len), short_size
                        img_resize = cv2.resize(src_img, (new_w, new_h))

                        resized_img, det_boxes_r_, det_scores_r_, det_category_r_ = \
                            sess.run(
                                [img_batch, detection_boxes, detection_scores, detection_category],
                                feed_dict={img_plac: img_resize[:, :, ::-1]}
                            )

                        resized_h, resized_w = resized_img.shape[1], resized_img.shape[2]
                        src_h, src_w = src_img.shape[0], src_img.shape[1]

                        if len(det_boxes_r_) > 0:
                            det_boxes_r_ = forward_convert(det_boxes_r_, False)
                            det_boxes_r_[:, 0::2] *= (src_w / resized_w)
                            det_boxes_r_[:, 1::2] *= (src_h / resized_h)
                            # det_boxes_r_ = backward_convert(det_boxes_r_, False)

                            for ii in range(len(det_boxes_r_)):
                                box_rotate = det_boxes_r_[ii]
                                box_rotate[0::2] = box_rotate[0::2] + ww_
                                box_rotate[1::2] = box_rotate[1::2] + hh_
                                box_res_rotate.append(box_rotate)
                                label_res_rotate.append(det_category_r_[ii])
                                score_res_rotate.append(det_scores_r_[ii])

            box_res_rotate = np.array(box_res_rotate)
            label_res_rotate = np.array(label_res_rotate)
            score_res_rotate = np.array(score_res_rotate)

            filter_indices = score_res_rotate >= 0.05
            score_res_rotate = score_res_rotate[filter_indices]
            box_res_rotate = box_res_rotate[filter_indices]
            label_res_rotate = label_res_rotate[filter_indices]

            box_res_rotate_ = []
            label_res_rotate_ = []
            score_res_rotate_ = []
            threshold = {'roundabout': 0.1, 'tennis-court': 0.3, 'swimming-pool': 0.1, 'storage-tank': 0.2,
                         'soccer-ball-field': 0.3, 'small-vehicle': 0.2, 'ship': 0.2, 'plane': 0.3,
                         'large-vehicle': 0.1, 'helicopter': 0.2, 'harbor': 0.0001, 'ground-track-field': 0.3,
                         'bridge': 0.0001, 'basketball-court': 0.3, 'baseball-diamond': 0.3,
                         'A': 0.7,'B': 0.7,'C':0.6,'D':0.7,'E':0.7,'F':0.7,'G':0.7,'H':0.7,'I':0.7, 'J':0.7,
                        'K':0.7,'L':0.7, 'M':0.7,'N':0.7,'O':0.8,'P':0.8,'Q':0.9, 'R':0.8,'S':0.7,'T':0.7,
                        'U':0.7,'V':0.7,'W':0.7, 'X':0.7,'Y':0.8,'Z':0.8, '1':0.5, '2':0.7,'3':0.7, '4':0.7,
                        '5':0.7,'6':0.7,'7':0.7,'8':0.7,'9':0.7,'0':0.7,
                        'AA': 0.7,'BB': 0.7,'CC':0.7,'DD':0.7,'EE':0.7,'FF':0.7,'GG':0.7,'HH':0.7,'II':0.7, 'JJ':0.7,
                        'KK':0.7,'LL':0.7, 'MM':0.7,'NN':0.7,'OO':0.8,'PP':0.8,'QQ':0.9, 'RR':0.8,'SS':0.7,'TT':0.7,
                        'UU':0.7,'VV':0.7,'WW':0.7, 'XX':0.7,'YY':0.8,'ZZ':0.8, '11':0.7, '22':0.7,'33':0.7, '44':0.7,
                        '55':0.7,'66':0.7,'77':0.7,'88':0.7,'99':0.7,'00':0.7}

            for sub_class in range(1, cfgs.CLASS_NUM + 1):
                index = np.where(label_res_rotate == sub_class)[0]
                if len(index) == 0:
                    continue
                tmp_boxes_r = box_res_rotate[index]
                tmp_label_r = label_res_rotate[index]
                tmp_score_r = score_res_rotate[index]

                tmp_boxes_r_ = backward_convert(tmp_boxes_r, False)

                try:
                    inx = nms_rotate.nms_rotate_cpu(boxes=np.array(tmp_boxes_r_),
                                                    scores=np.array(tmp_score_r),
                                                    iou_threshold=threshold[LABEL_NAME_MAP[sub_class]],
                                                    max_output_size=5000)
                except:
                    tmp_boxes_r_ = np.array(tmp_boxes_r_)
                    tmp = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                    tmp[:, 0:-1] = tmp_boxes_r_
                    tmp[:, -1] = np.array(tmp_score_r)
                    # Note: the IoU of two same rectangles is 0, which is calculated by rotate_gpu_nms
                    jitter = np.zeros([tmp_boxes_r_.shape[0], tmp_boxes_r_.shape[1] + 1])
                    jitter[:, 0] += np.random.rand(tmp_boxes_r_.shape[0], ) / 1000
                    inx = rotate_gpu_nms(np.array(tmp, np.float32) + np.array(jitter, np.float32),
                                         float(threshold[LABEL_NAME_MAP[sub_class]]), 0)

                box_res_rotate_.extend(np.array(tmp_boxes_r)[inx])
                score_res_rotate_.extend(np.array(tmp_score_r)[inx])
                label_res_rotate_.extend(np.array(tmp_label_r)[inx])

            result_dict = {'boxes': np.array(box_res_rotate_), 'scores': np.array(score_res_rotate_),
                           'labels': np.array(label_res_rotate_), 'image_id': img_path}
            result_queue.put_nowait(result_dict)


def test_dota(det_net, real_test_img_list, args, txt_name):

    save_path = os.path.join('./test_OCR_Latest1', cfgs.VERSION)

    nr_records = len(real_test_img_list)
    pbar = tqdm(total=nr_records)
    gpu_num = len(args.gpus.strip().split(','))

    nr_image = math.ceil(nr_records / gpu_num)
    result_queue = Queue(500)
    procs = []

    for i, gpu_id in enumerate(args.gpus.strip().split(',')):
        start = i * nr_image
        end = min(start + nr_image, nr_records)
        split_records = real_test_img_list[start:end]
        proc = Process(target=worker, args=(int(gpu_id), split_records, det_net, args, result_queue))
        print('process:%d, start:%d, end:%d' % (i, start, end))
        proc.start()
        procs.append(proc)

    for i in range(nr_records):
        res = result_queue.get()
    # for score in res['scores']:
    #     if score >= 0.9:
    #         print(score)
        if args.show_box:
            # print(res['scores'])
            # print(type(res))
            nake_name = res['image_id'].split('/')[-1]
            tools.mkdir(os.path.join(save_path, 'dota_img_vis'))
            draw_path = os.path.join(save_path, 'dota_img_vis', nake_name)

            draw_img = np.array(cv2.imread(res['image_id']), np.float32)
            detected_boxes = backward_convert(res['boxes'], with_label=False)

            detected_indices = res['scores'] >= cfgs.SHOW_SCORE_THRSHOLD
            detected_scores = res['scores'][detected_indices]
            detected_boxes = detected_boxes[detected_indices]
            detected_categories = res['labels'][detected_indices]
            for score in detected_scores:
                if(score >= 0.9):
                    print(score)
                    final_detections = draw_box_in_img.draw_boxes_with_label_and_scores(draw_img,boxes=detected_boxes,labels=detected_categories,scores=detected_scores,method=1,in_graph=False)
                    cv2.imwrite(draw_path, final_detections)

        else:
            CLASS_DOTA = NAME_LABEL_MAP.keys()
            write_handle = {}

            tools.mkdir(os.path.join(save_path, 'dota_res'))
            for sub_class in CLASS_DOTA:
                if sub_class == 'back_ground':
                    continue
                write_handle[sub_class] = open(os.path.join(save_path, 'dota_res', 'Task1_%s.txt' % sub_class), 'a+')

            # rboxes = forward_convert(res['boxes'], with_label=False)

            for i, rbox in enumerate(res['boxes']):
                command = '%s %.3f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f\n' % (res['image_id'].split('/')[-1].split('.')[0],
                                                                                 res['scores'][i],
                                                                                 rbox[0], rbox[1], rbox[2], rbox[3],
                                                                                 rbox[4], rbox[5], rbox[6], rbox[7],)
                write_handle[LABEL_NAME_MAP[res['labels'][i]]].write(command)

            for sub_class in CLASS_DOTA:
                if sub_class == 'back_ground':
                    continue
                write_handle[sub_class].close()

            fw = open(txt_name, 'a+')
            fw.write('{}\n'.format(res['image_id'].split('/')[-1]))
            fw.close()

        pbar.set_description("Test image %s" % res['image_id'].split('/')[-1])

        pbar.update(1)

    for p in procs:
        p.join()


def eval(num_imgs, args):

    txt_name = '{}.txt'.format(cfgs.VERSION)
    if not args.show_box:
        if not os.path.exists(txt_name):
            fw = open(txt_name, 'w')
            fw.close()

        fr = open(txt_name, 'r')
        img_filter = fr.readlines()
        print('****************************'*3)
        print('Already tested imgs:', img_filter)
        print('****************************'*3)
        fr.close()

        test_imgname_list = [os.path.join(args.test_dir, img_name) for img_name in os.listdir(args.test_dir)
                             if img_name.endswith(('.bmp','.jpg', '.png', '.jpeg', '.tif', '.tiff')) and
                             (img_name + '\n' not in img_filter)]
    else:
        test_imgname_list = [os.path.join(args.test_dir, img_name) for img_name in os.listdir(args.test_dir)
                             if img_name.endswith(('.bmp','.jpg', '.png', '.jpeg', '.tif', '.tiff'))]

    assert len(test_imgname_list) != 0, 'test_dir has no imgs there.' \
                                        ' Note that, we only support img format of (.jpg, .png, and .tiff) '

    if num_imgs == np.inf:
        real_test_img_list = test_imgname_list
    else:
        real_test_img_list = test_imgname_list[: num_imgs]

    fpn = build_whole_network.DetectionNetwork(
        base_network_name=cfgs.NET_NAME,
        is_training=False)
    test_dota(det_net=fpn, real_test_img_list=real_test_img_list, args=args, txt_name=txt_name)

    if not args.show_box:
        os.remove(txt_name)


def parse_args():

    parser = argparse.ArgumentParser('evaluate the result.')

    parser.add_argument('--test_dir', dest='test_dir',
                        help='evaluate imgs dir ',
                        default='../data/dataset/OCR/RafI_Latest_OCR/test/images/', type=str)
    parser.add_argument('--gpus', dest='gpus',
                        help='gpu id',
                        default='0,1', type=str)
    parser.add_argument('--eval_num', dest='eval_num',
                        help='the num of eval imgs',
                        default=np.inf, type=int)
    parser.add_argument('--show_box', '-s', default=True,
                        action='store_true')
    parser.add_argument('--multi_scale', '-ms', default=True,
                        action='store_true')
    parser.add_argument('--h_len', dest='h_len',
                        help='image height',
                        default=800, type=int)
    parser.add_argument('--w_len', dest='w_len',
                        help='image width',
                        default=800, type=int)
    parser.add_argument('--h_overlap', dest='h_overlap',
                        help='height overlap',
                        default=200, type=int)
    parser.add_argument('--w_overlap', dest='w_overlap',
                        help='width overlap',
                        default=200, type=int)
    args = parser.parse_args()
    return args


if __name__ == '__main__':

    args = parse_args()
    print(20*"--")
    print(args)
    print(20*"--")
    eval(args.eval_num,
         args=args)


