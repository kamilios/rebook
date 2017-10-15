import cv2
import math
import numpy as np

from lib import debug_imwrite
from binarize import binarize

cross33 = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
def gradient(im):
    return cv2.morphologyEx(im, cv2.MORPH_GRADIENT, cross33)

def hsl_gray(im):
    assert len(im.shape) == 3
    hls = cv2.cvtColor(im, cv2.COLOR_RGB2HLS)
    _, l, s = cv2.split(hls)
    return s, l

def text_contours(im, original):
    im_h, im_w = im.shape
    min_feature_size = im_h / 300

    copy = im.copy()
    cv2.rectangle(copy, (0, 0), (im_w, im_h), 255, 3)
    _, contours, [hierarchy] = \
        cv2.findContours(copy, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    # find biggest holes
    image_area = im_w * im_h
    good_holes = []
    i = 0
    while i >= 0:
        j = hierarchy[i][2]
        while j >= 0:
            c = contours[j]
            x, y, w, h = cv2.boundingRect(c)
            if w * h > image_area * 0.25:
                good_holes.append(j)
            j = hierarchy[j][0]
        i = hierarchy[i][0]

    good_contours, bad_contours = [], []
    for hole in good_holes:
        x, y, w, h = cv2.boundingRect(contours[hole])
        # print "hole:", x, y, w, h

        i = hierarchy[hole][2]
        while i >= 0:
            c = contours[i]
            x, y, w, h = cv2.boundingRect(c)
            # print 'mean:', orig_slice.mean(), \
            # 'horiz stddev:', orig_slice.mean(axis=0).std()
            # print 'contour:', x, y, w, h
            if len(c) > 10 \
                    and h < 2 * w \
                    and w > min_feature_size \
                    and h > min_feature_size:
                mask = np.zeros((h, w), dtype=np.uint8)
                cv2.drawContours(mask, contours, i, 255,
                                 thickness=cv2.FILLED,
                                 offset=(-x, -y))
                mask_filled = np.count_nonzero(mask)

                orig_slice = cv2.bitwise_not(original[y:y + h, x:x + w])
                orig_filled = np.count_nonzero(mask & orig_slice)

                filled_ratio = orig_filled / float(mask_filled)
                if filled_ratio > 0.1:
                    good_contours.append(c)
            else:
                bad_contours.append(c)
            i = hierarchy[i][0]

    mask = np.zeros(im.shape, dtype=np.uint8)
    for i in range(len(good_contours)):
        cv2.drawContours(mask, good_contours, i, 255,
                         thickness=cv2.FILLED)
    debug_imwrite('text_contours.png', mask)

    return good_contours, bad_contours

# and x > 0.02 * im_w \
# and x + w < 0.98 * im_w \
# and y > 0.02 * im_h \
# and y + h < 0.98 * im_h:

def _line_contours(im):
    im_h, _ = im.shape

    first_pass = binarize(im, resize=1000.0 / im_h)

    grad = gradient(first_pass)
    space_width = (im_h / 50) | 1
    line_height = (im_h / 400) | 1
    horiz = cv2.getStructuringElement(cv2.MORPH_RECT,
                                      (space_width, line_height))
    grad = cv2.morphologyEx(grad, cv2.MORPH_CLOSE, horiz)

    line_contours, _ = text_contours(grad, first_pass)

    return first_pass, line_contours

def skew_angle(im):
    small_bw, line_contours = _line_contours(im)

    lines = cv2.cvtColor(small_bw, cv2.COLOR_GRAY2RGB)
    alphas = []
    for c in line_contours:
        x, y, w, h = cv2.boundingRect(c)
        if w > 4 * h:
            vx, vy, x1, y1 = cv2.fitLine(c, cv2.DIST_L2, 0, 0.01, 0.01)
            cv2.line(lines,
                     (x1 - vx * 10000, y1 - vy * 10000),
                     (x1 + vx * 10000, y1 + vy * 10000),
                     (255, 0, 0), thickness=3)
            alphas.append(math.atan2(vy, vx))

    debug_imwrite('lines.png', lines)
    return np.median(alphas)

def dewarp_text(im):
    small_bw, contours = _line_contours(im)

def safe_rotate(im, angle):
    debug_imwrite('prerotated.png', im)
    im_h, im_w = im.shape
    if abs(angle) > math.pi / 4:
        print "warning: too much rotation"
        return im

    im_h_new = im_w * abs(math.sin(angle)) + im_h * math.cos(angle)
    im_w_new = im_h * abs(math.sin(angle)) + im_w * math.cos(angle)

    pad_h = int(math.ceil((im_h_new - im_h) / 2))
    pad_w = int(math.ceil((im_w_new - im_w) / 2))

    padded = np.pad(im, (pad_h, pad_w), 'constant', constant_values=255)
    padded_h, padded_w = padded.shape
    angle_deg = angle * 180 / math.pi
    print 'rotating to angle:', angle_deg, 'deg'
    matrix = cv2.getRotationMatrix2D((padded_w / 2, padded_h / 2), angle_deg, 1)
    result = cv2.warpAffine(padded, matrix, (padded_w, padded_h),
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=255)
    debug_imwrite('rotated.png', result)
    return result
