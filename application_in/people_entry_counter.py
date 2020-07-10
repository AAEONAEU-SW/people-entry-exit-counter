"""Restricted Zone Notifier."""

"""
 Copyright (c) 2018 Intel Corporation.

 Permission is hereby granted, free of charge, to any person obtaining
 a copy of this software and associated documentation files (the
 "Software"), to deal in the Software without restriction, including
 without limitation the rights to use, copy, modify, merge, publish,
 distribute, sublicense, and/or sell copies of the Software, and to
 permit person to whom the Software is furnished to do so, subject to
 the following conditions:

 The above copyright notice and this permission notice shall be
 included in all copies or substantial portions of the Software.

 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""

import os
import sys
import json
import time
import socket
import cv2

import logging as log
import paho.mqtt.client as mqtt

from threading import Thread
from collections import namedtuple
from argparse import ArgumentParser
from inference import Network

# entrancestatus contains information about entry area
INFO = True
Person_count = 0

# MQTT server environment variables
HOSTNAME = socket.gethostname()
IPADDRESS = socket.gethostbyname(HOSTNAME)
TOPIC = "People_entry_exit_counter"
MQTT_HOST = IPADDRESS
MQTT_PORT = 1883
MQTT_KEEPALIVE_INTERVAL = 60

# Global variables
TARGET_DEVICE = 'GPU'
accepted_devices = ['CPU', 'GPU', 'MYRIAD', 'HETERO:FPGA,CPU', 'HDDL']
is_async_mode = True
CONFIG_FILE = '../resources/config.json'

# Flag to control background thread
KEEP_RUNNING = True

DELAY = 5


def build_argparser():
    """
    Parse command line arguments.

    :return: Command line arguments
    """
    parser = ArgumentParser()
    parser.add_argument("-m", "--model", required=True, type=str,
                        help="Path to an .xml file with a trained model.")
    parser.add_argument("-l", "--cpu_extension", type=str, default=None,
                        help="MKLDNN (CPU)-targeted custom layers. Absolute "
                             "path to a shared library with the kernels impl.")
    parser.add_argument("-d", "--device", default="CPU", type=str,
                        help="Specify the target device to infer on; "
                             "CPU, GPU, FPGA, HDDL, MYRIAD is acceptable. To run with multiple devices use "
                             "MULTI:<device1>,<device2>,etc. Application "
                             "will look for a suitable plugin for device specified"
                             "(CPU by default)")
    parser.add_argument("-th", "--prob_threshold", default=0.5, type=float,
                        help="Probability threshold for detections filtering")
    parser.add_argument('-x', '--pointx', default=0, type=int,
                        help="X coordinate of the top left point of Entry"
                             " area on camera feed.")
    parser.add_argument('-y', '--pointy', default=0, type=int,
                        help="Y coordinate of the top left point of Entry"
                             " area on camera feed.")
    parser.add_argument('-w', '--width', default=0, type=int,
                        help="Width of the assembly area in pixels.")
    parser.add_argument('-ht', '--height', default=0, type=int,
                        help="Height of the assembly area in pixels.")
    parser.add_argument('-r', '--rate', default=1, type=int,
                        help="Number of seconds between data updates "
                             "to MQTT server")
    parser.add_argument("-f", "--flag", help="sync or async", default="async", type=str)

    global TARGET_DEVICE, is_async_mode
    args = parser.parse_args()
    if args.device:
        TARGET_DEVICE = args.device
    if args.flag == "sync":
        is_async_mode = False
    else:
        is_async_mode = True
    return parser


def check_args():
    # ArgumentParser checks the device

    global TARGET_DEVICE
    if 'MULTI' not in TARGET_DEVICE and TARGET_DEVICE not in accepted_devices:
        print("Unsupported device: " + TARGET_DEVICE)
        sys.exit(2)
    elif 'MULTI' in TARGET_DEVICE:
        target_devices = TARGET_DEVICE.split(':')[1].split(',')
        for multi_device in target_devices:
            if multi_device not in accepted_devices:
                print("Unsupported device: " + TARGET_DEVICE)
                sys.exit(2)


def ssd_out(res, args, initial_wh, selected_region):
    """
    Parse SSD output.

    :param res: Detection results
    :param args: Parsed arguments
    :param initial_wh: Initial width and height of the frame
    :param selected_region: Selected region coordinates
    :return: None
    """
    global INFO
    person = []
    INFO = True

    for obj in res[0][0]:
        # Draw objects only when probability is more than specified threshold
        if obj[2] > args.prob_threshold:
            xmin = int(obj[3] * initial_wh[0])
            ymin = int(obj[4] * initial_wh[1])
            xmax = int(obj[5] * initial_wh[0])
            ymax = int(obj[6] * initial_wh[1])
            person.append([xmin, ymin, xmax, ymax])

    for p in person:
        # area_of_person gives area of the detected person
        area_of_person = (p[2] - p[0]) * (p[3] - p[1])
        x_max = max(p[0], selected_region[0])
        x_min = min(p[2], selected_region[0] + selected_region[2])
        y_min = min(p[3], selected_region[1] + selected_region[3])
        y_max = max(p[1], selected_region[1])
        point_x = x_min - x_max
        point_y = y_min - y_max
        # area_of_intersection gives area of intersection of the
        # detected person and the selected area
        area_of_intersection = point_x * point_y
        if point_x < 0 or point_y < 0:
            continue
        else:
            if area_of_person > area_of_intersection:
                # Entry line area flags
                INFO = True
            else:
                # Entry area flags
                INFO = False


def message_runner():
    """
    Publish Person status to MQTT topic.
    Pauses for rate second(s) between updates

    :return: None
    """
    while KEEP_RUNNING:
        time.sleep(1)
        if INFO is True:
                Person_count = 1
        else:
                Person_count = 0
        CLIENT.publish(TOPIC, payload=json.dumps({"Person": Person_count}))


def main():
    """
    Load the network and parse the output.

    :return: None
    """
    global DELAY
    global CLIENT
    global SIG_CAUGHT
    global KEEP_RUNNING
    global TARGET_DEVICE
    global is_async_mode
    CLIENT = mqtt.Client()
    CLIENT.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE_INTERVAL)
    CLIENT.subscribe(TOPIC)
    log.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=log.INFO, stream=sys.stdout)
    args = build_argparser().parse_args()
    logger = log.getLogger()
    render_time = 0
    roi_x = args.pointx
    roi_y = args.pointy
    roi_w = args.width
    roi_h = args.height
    check_args()

    assert os.path.isfile(CONFIG_FILE), "{} file doesn't exist".format(CONFIG_FILE)
    config = json.loads(open(CONFIG_FILE).read())

    for idx, item in enumerate(config['inputs']):
        if item['video'].isdigit():
            input_stream = int(item['video'])
        else:
            input_stream = item['video']

    cap = cv2.VideoCapture(input_stream)
    if not cap.isOpened():
        logger.error("ERROR! Unable to open video source")
        sys.exit(1)

    # Init inference request IDs
    cur_request_id = 0
    next_request_id = 1

    # Initialise the class
    infer_network = Network()
    # Load the network to IE plugin to get shape of input layer
    n, c, h, w = infer_network.load_model(args.model, TARGET_DEVICE, 1, 1, 2, args.cpu_extension)[1]

    message_thread = Thread(target=message_runner, args=())
    message_thread.setDaemon(True)
    message_thread.start()

    if is_async_mode:
        print("Application running in async mode...")
    else:
        print("Application running in sync mode...")

    ret, frame = cap.read()
    while ret:

        ret, next_frame = cap.read()
        if not ret:
            KEEP_RUNNING = False
            break

        initial_wh = [cap.get(3), cap.get(4)]

        if next_frame is None:
            KEEP_RUNNING = False
            log.error("ERROR! blank FRAME grabbed")
            break

        # If either default values or negative numbers are given,
        # then we will default to start of the FRAME
        if roi_x <= 0 or roi_y <= 0:
            roi_x = 0
            roi_y = 0
        if roi_w <= 0:
            roi_w = next_frame.shape[1]
        if roi_h <= 0:
            roi_h = next_frame.shape[0]
        key_pressed = cv2.waitKey(1)

        # 'c' key pressed
        if key_pressed == 99:
            # Give operator chance to change the area
            # Select rectangle from left upper corner, dont display crosshair
            ROI = cv2.selectROI("Entry Selection", frame, True, False)
            print("Entry Area Selection: -x = {}, -y = {}, -w = {},"
                  " -h = {}".format(ROI[0], ROI[1], ROI[2], ROI[3]))
            roi_x = ROI[0]
            roi_y = ROI[1]
            roi_w = ROI[2]
            roi_h = ROI[3]
            cv2.destroyAllWindows()

        cv2.rectangle(frame, (roi_x, roi_y),
                      (roi_x + roi_w, roi_y + roi_h), (0, 0, 255), 2)
        selected_region = [roi_x, roi_y, roi_w, roi_h]

        in_frame_fd = cv2.resize(next_frame, (w, h))
        # Change data layout from HWC to CHW
        in_frame_fd = in_frame_fd.transpose((2, 0, 1))
        in_frame_fd = in_frame_fd.reshape((n, c, h, w))

        # Start asynchronous inference for specified request.
        inf_start = time.time()
        if is_async_mode:
            # Async enabled and only one video capture
            infer_network.exec_net(next_request_id, in_frame_fd)
        else:
            # Async disabled
            infer_network.exec_net(cur_request_id, in_frame_fd)
        # Wait for the result
        infer_network.wait(cur_request_id)
        det_time = time.time() - inf_start
        # Results of the output layer of the network
        res = infer_network.get_output(cur_request_id)
        # Parse SSD output
        ssd_out(res, args, initial_wh, selected_region)

        # Draw performance stats
        inf_time_message = "Inference time: N\A for async mode" if is_async_mode else \
            "Inference time: {:.3f} ms".format(det_time * 1000)
        render_time_message = "OpenCV rendering time: {:.3f} ms". \
            format(render_time * 1000)

        if not INFO:
            warning = "PERSON IN"
            cv2.putText(frame, warning, (15, 100), cv2.FONT_HERSHEY_COMPLEX, 0.8, (0, 0, 255), 2)

        log_message = "Async mode is on." if is_async_mode else \
            "Async mode is off."
        cv2.putText(frame, log_message, (15, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, inf_time_message, (15, 35), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, render_time_message, (15, 55), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, "Person still outside: {}".format(INFO), (15, 75), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)

        render_start = time.time()
        cv2.imshow("Entry Zone Notifier", frame)
        render_end = time.time()
        render_time = render_end - render_start

        frame = next_frame

        if key_pressed == 27:
            print("Attempting to stop background threads")
            KEEP_RUNNING = False
            break
            # Tab key pressed
        if key_pressed == 9:
            is_async_mode = not is_async_mode
            print("Switched to {} mode".format("async" if is_async_mode else "sync"))

        if is_async_mode:
            # Swap infer request IDs
            cur_request_id, next_request_id = next_request_id, cur_request_id

    infer_network.clean()
    message_thread.join()
    cap.release()
    cv2.destroyAllWindows()
    CLIENT.disconnect()


if __name__ == '__main__':
    main()




