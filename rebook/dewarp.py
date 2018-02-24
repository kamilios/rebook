from __future__ import print_function

import cv2
import itertools
import numpy as np
import scipy
import sys

from math import sqrt, cos, sin, acos, atan2, pi
from numpy import dot, newaxis
from numpy.linalg import norm, inv, solve
from numpy.polynomial import Polynomial as Poly
from scipy import interpolate
from scipy import optimize as opt
from scipy.linalg import block_diag
from scipy.ndimage import grey_dilation
from skimage.measure import ransac

import algorithm
import binarize
import collate
import crop
from geometry import Crop, Line
import lib
from lib import RED, GREEN, BLUE, draw_circle, draw_line
import newton

# focal length f = 3270.5 pixels
f = 3270.5
Of = np.array([0, 0, f], dtype=np.float64)

def compress(l, flags):
    return list(itertools.compress(l, flags))

def peak_points(l, AH):
    x_min, x_max = l[0][1], l[-1][1] + l[-1][3]
    y_min = min([y for c, x, y, w, h in l]) + 1
    y_max = max([y + h for c, x, y, w, h in l]) + 1
    height, width = y_max - y_min, x_max - x_min

    mask = np.zeros((y_max - y_min, x_max - x_min))
    contours = [c for c, x, y, w, h in l]
    cv2.drawContours(mask, contours, -1, 255, thickness=cv2.FILLED,
                        offset=(-x_min, -y_min))

    old_bottom = height - mask[::-1].argmax(axis=0)
    good_bottoms = mask.max(axis=0) > 0
    bottom_xs, = np.where(good_bottoms)
    bottom_ys = old_bottom[good_bottoms]
    bottom = np.interp(np.arange(width), bottom_xs, bottom_ys)
    assert (bottom[good_bottoms] == old_bottom[good_bottoms]).all()

    delta = AH / 2
    peaks = grey_dilation(bottom, size=2 * delta + 1)
    bottom_points = np.array(list(zip(list(range(width)), bottom)))
    peak_points = bottom_points[bottom_points[:, 1] == peaks]
    return peak_points

class PolyModel5(object):
    def estimate(self, data):
        self.params = Poly.fit(data[:, 0], data[:, 1], 5, domain=[-1, 1])
        return True

    def residuals(self, data):
        return abs(self.params(data[:, 0]) - data[:, 1])

def trace_baseline(im, line, color=BLUE):
    domain = np.linspace(line.left() - 100, line.right() + 100, 200)
    points = np.vstack([domain, line.model(domain)]).T
    for p1, p2 in zip(points, points[1:]):
        draw_line(im, p1, p2, color=color, thickness=1)

def merge_lines(AH, lines):
    out_lines = [lines[0]]

    for line in lines[1:]:
        x_min, x_max = line[0].left(), line[-1].right()
        integ = (out_lines[-1].model - line.model).integ()
        if abs(integ(x_max) - integ(x_min)) / (x_max - x_min) < AH / 8.0:
            out_lines[-1].merge(line)
            points = np.array([letter.base_point() for letter in out_lines[-1]])
            new_model, inliers = ransac(points, PolyModel5, 10, AH / 15.0)
            out_lines[-1].compress(inliers)
            out_lines[-1].model = new_model.params
        else:
            out_lines.append(line)

    # debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2RGB)
    # for l in out_lines:
    #     trace_baseline(debug, l, BLUE)
    # lib.debug_imwrite('merged.png', debug)

    print('original lines:', len(lines), 'merged lines:', len(out_lines))
    return out_lines

@lib.timeit
def remove_outliers(im, AH, lines):
    debug = cv2.cvtColor(im, cv2.COLOR_GRAY2RGB)

    result = []
    for l in lines:
        if len(l) < 5: continue

        points = np.array([letter.base_point() for letter in l])
        model, inliers = ransac(points, PolyModel5, 10, AH / 10.0)
        poly = model.params
        l.model = poly
        # trace_baseline(debug, l, BLUE)
        for p, is_in in zip(points, inliers):
            color = GREEN if is_in else RED
            draw_circle(debug, p, 4, color=color)

        l.compress(inliers)
        result.append(l)

    for l in result:
        draw_circle(debug, l[0].left_mid(), 6, BLUE, -1)
        draw_circle(debug, l[-1].right_mid(), 6, BLUE, -1)

    lib.debug_imwrite('lines.png', debug)
    return merge_lines(AH, result)

# x = my + b model weighted t
class LinearXModel(object):
    def estimate(self, data):
        self.params = Poly.fit(data[:, 1], data[:, 0], 1, domain=[-1, 1])
        return True

    def residuals(self, data):
        return abs(self.params(data[:, 1]) - data[:, 0])

def side_lines(AH, lines):
    im_h, _ = bw.shape

    left_bounds = np.array([l[0].left_mid() for l in lines])
    right_bounds = np.array([l[-1].right_mid() for l in lines])

    vertical_lines = []
    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for coords in [left_bounds, right_bounds]:
        model, inliers = ransac(coords, LinearXModel, 3, AH / 10.0)
        vertical_lines.append(model.params)
        for p, inlier in zip(coords, inliers):
            draw_circle(debug, p, 4, color=GREEN if inlier else RED)

    for p in vertical_lines:
        draw_line(debug, (p(0), 0), (p(im_h), im_h), BLUE, 2)
    lib.debug_imwrite('vertical.png', debug)

    return vertical_lines

def estimate_vanishing(AH, lines):
    p_left, p_right = side_lines(AH, lines)
    vy, = (p_left - p_right).roots()
    return np.array((p_left(vy), vy))

def centroid(poly, line):
    first, last = line[0], line[-1]
    _, x0, _, w0, _ = first
    _, x1, _, w1, _ = last
    domain = np.linspace(x0, x1 + w1, 20)
    points = np.vstack([domain, poly(domain)]).T
    return points.mean(axis=0)

def plot_norm(points, *args, **kwargs):
    norm = points - points[0]
    norm /= norm[-1][0]
    norm_T = norm.T
    norm_T[1] -= norm_T[1][0]
    # norm_T[1] /= norm_T[1].max()
    plt.plot(norm_T[0], norm_T[1], *args, **kwargs)

def C0_C1(lines, v):
    _, vy = v
    # use bottom line as C0 if vanishing point above image
    C0, C1 = (lines[-1], lines[0]) if vy < 0 else (lines[0], lines[-1])
    return C0, C1

def widest_domain(lines, v, n_points):
    C0, C1 = C0_C1(lines, v)

    v_lefts = [Line.from_points(v, l[0].left_bot()) for l in lines if l is not C0]
    v_rights = [Line.from_points(v, l[-1].right_bot()) for l in lines if l is not C0]
    C0_lefts = [l.text_line_intersect(C0)[0] for l in v_lefts]
    C0_rights = [l.text_line_intersect(C0)[0] for l in v_rights]

    x_min = min(C0.left(), min(C0_lefts))
    x_max = max(C0.left(), max(C0_rights))
    domain = np.linspace(x_min, x_max, n_points)

    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for l in lines:
        cv2.line(debug, tuple(l[0].left_bot().astype(int)),
                tuple(l[-1].right_bot().astype(int)), GREEN, 2)
    Line.from_points(v, (x_min, C0(x_min))).draw(debug)
    Line.from_points(v, (x_max, C0(x_max))).draw(debug)
    lib.debug_imwrite('domain.png', debug)

    return domain, C0, C1

def arc_length_points(xs, ys, n_points):
    arc_points = np.stack((xs, ys))
    arc_lengths = norm(np.diff(arc_points, axis=1), axis=0)
    cumulative_arc = np.hstack([[0], np.cumsum(arc_lengths)])
    D = interpolate.interp1d(cumulative_arc, arc_points, assume_sorted=True)

    total_arc = cumulative_arc[-1]
    print('total D arc length:', total_arc)
    s_domain = np.linspace(0, total_arc, n_points)
    return D(s_domain), total_arc

N_POINTS = 200
MU = 30
def estimate_directrix(lines, v, n_points_w):
    vx, vy = v

    domain, C0, C1 = widest_domain(lines, v, N_POINTS)

    C0_points = np.vstack([domain, C0(domain)])
    longitudes = [Line.from_points(v, p) for p in C0_points.T]
    C1_points = np.array([l.closest_poly_intersect(C1.model, p) \
                          for l, p in zip(longitudes, C0_points.T)]).T
    lambdas = (vy - C0_points[1]) / (C1_points[1] - C0_points[1])
    alphas = MU * lambdas / (MU + lambdas - 1)
    C_points = (1 - alphas) * C0_points + alphas * C1_points
    C = C_points.T.mean(axis=0)

    theta = acos(f / sqrt(vx ** 2 + vy ** 2 + f ** 2))
    print('theta:', theta)
    A = np.array([
        [1, C[0] / f * -sin(theta)],
        [0, cos(theta) - C[1] / f * sin(theta)]
    ])

    D_points = inv(A).dot(C_points)
    D_points_arc, _ = arc_length_points(D_points)
    C_points_arc = A.dot(D_points_arc)

    # plot_norm(np.vstack([domain, C0(domain)]).T, label='C0')
    # plot_norm(np.vstack([domain, C1(domain)]).T, label='C1')
    # plot_norm(C_points.T, label='C20')
    # plot_norm(D_points.T, label='D')
    # plot_norm(C_points_arc.T, label='C')
    # # plt.plot(C_points.T, label='C20')
    # # plt.plot(C_points_arc.T, label='C')
    # plt.axes().legend()
    # plt.show()

    return D_points_arc, C_points_arc

def aspect_ratio(im, lines, D, v, O):
    vx, vy = v
    C0, C1 = C0_C1(lines, v)

    im_h, im_w = im.shape

    m = -(vx - O[0]) / (vy - O[1])
    L0 = Line.from_point_slope(C0.first_base(), m)
    L1 = Line.from_point_slope(C1.first_base(), m)
    perp = L0.altitude(v)
    p0, p1 = L0.intersect(perp), L1.intersect(perp)
    h_img = norm(p0 - p1)

    L = Line(m, -m * O[0] - (f ** 2) / (vy - O[1]))
    F = L.altitude(v).intersect(L)
    _, x0r, y0r, w0r, h0r = lines[-1][-1]
    p0r = np.array([x0r + w0r / 2.0, y0r + h0r])
    F_C0r = Line.from_points(F, p0r)
    q0 = F_C0r.intersect(L0)
    l_img = norm(q0 - p0)

    debug = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    L0.draw(debug)
    L1.draw(debug)
    L.draw(debug, color=GREEN)
    F_C0r.draw(debug, color=RED)
    lib.debug_imwrite('aspect.png', debug)

    # Convergence line perp to V=(vx, vy, f)
    # y = -vx / vy * x + -f^2 / vy
    alpha = atan2(norm(p1 - O), f)
    theta = acos(f / sqrt((vx - O[0]) ** 2 + (vy - O[1]) ** 2 + f ** 2))
    beta = pi / 2 - theta

    lp_img = abs(D[0][-1] - D[0][0])
    wp_img = norm(np.diff(D.T, axis=0), axis=1).sum()
    print('h_img:', h_img, 'l\'_img:', lp_img, 'alpha:', alpha)
    print('l_img:', l_img, 'w\'_img:', wp_img, 'beta:', beta)
    r = h_img * lp_img * cos(alpha) / (l_img * wp_img * cos(alpha + beta))

    return r

class MuMode(object):
    def __init__(self, val):
        self.val = val

    def __eq__(self, other):
        return self.val == other.val

    def index(self):
        return 0 if self.val else -1

    def point(self, l):
        if self.val:
            return l.top_point()  # + np.array([0, -20])
        else:
            return l.base_point()  # + np.array([0, 20])

MuMode.BOTTOM = MuMode(False)
MuMode.TOP = MuMode(True)

# find mu necessary to entirely cover line with mesh
def necessary_mu(C0, C1, v, all_lines, mode):
    vx, vy = v

    line = all_lines[mode.index()]
    points = np.array([mode.point(l) for l in line])
    for p in points:
        global mu_debug
        cv2.circle(mu_debug, tuple(p.astype(int)), 6, GREEN, -1)

    longitudes = [Line.from_points(v, p) for p in points]
    C0_points = np.array([l.text_line_intersect(C0) for l in longitudes]).T
    C1_points = np.array([l.text_line_intersect(C1) for l in longitudes]).T
    lambdas = (vy - C0_points[1]) / (C1_points[1] - C0_points[1])
    alphas = (points[:, 1] - C0_points[1]) / (C1_points[1] - C0_points[1])
    mus = alphas * (1 - lambdas) / (alphas - lambdas)

    return mus.max() + 0.01 if np.median(mus) >= 0.5 else mus.min() - 0.01

@lib.timeit
def generate_mesh(all_lines, lines, C_arc, v, n_points_h):
    vx, vy = v
    C_arc_T = C_arc.T

    C0, C1 = C0_C1(lines, v)

    # first, calculate necessary mu.
    global mu_debug
    mu_debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    mu_bottom = necessary_mu(C0, C1, v, all_lines, MuMode.BOTTOM)
    mu_top = necessary_mu(C0, C1, v, all_lines, MuMode.TOP)
    lib.debug_imwrite('mu.png', mu_debug)

    longitude_lines = [Line.from_points(v, p) for p in C_arc_T]
    longitudes = []
    mus = np.linspace(mu_top, mu_bottom, n_points_h)
    for l, C_i in zip(longitude_lines, C_arc_T):
        p0 = l.closest_poly_intersect(C0.model, C_i)
        p1 = l.closest_poly_intersect(C1.model, C_i)
        lam = (vy - p0[1]) / (p1[1] - p0[1])
        alphas = mus * lam / (mus + lam - 1)
        longitudes.append(np.outer(1 - alphas, p0) + np.outer(alphas, p1))

    result = np.array(longitudes)

    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for l in result[::50]:
        for p in l[::50]:
            cv2.circle(debug, tuple(p.astype(int)), 6, BLUE, -1)
    trace_baseline(debug, C0, RED)
    trace_baseline(debug, C1, RED)
    lib.debug_imwrite('mesh.png', debug)

    return np.array(longitudes).transpose(1, 0, 2)

@lib.timeit
def correct_geometry(orig, mesh, interpolation=cv2.INTER_LINEAR):
    # coordinates (u, v) on mesh -> mesh[u][v] = (x, y) in distorted image
    mesh32 = mesh.astype(np.float32)
    xmesh, ymesh = mesh32[:, :, 0], mesh32[:, :, 1]
    conv_xmesh, conv_ymesh = cv2.convertMaps(xmesh, ymesh, cv2.CV_16SC2)
    out = cv2.remap(orig, conv_xmesh, conv_ymesh, interpolation=interpolation,
                    borderMode=cv2.BORDER_REPLICATE)
    lib.debug_imwrite('corrected.png', out)

    return out

def spline_model(line):
    base_points = np.array([letter.base_point() for letter in line])
    _, indices = np.unique(base_points[:, 0], return_index=True)
    data = base_points[indices]
    return interpolate.UnivariateSpline(data[:, 0], data[:, 1])

def valid_curvature(line):
    if len(line) < 4: return True

    poly = spline_model(line)
    polyp = poly.derivative()
    polypp = polyp.derivative()

    x_range = line.left(), line.right()
    x_points = np.linspace(x_range[0], x_range[1], 50)

    curvature = abs(polypp(x_points)) / (1 + polyp(x_points) ** 2)  # ** 3/2
    # print 'curvature:', curvature.max()

    global curvature_debug
    for p in zip(x_points, poly(x_points)):
        cv2.circle(curvature_debug, (int(p[0]), int(p[1])), 2, BLUE, -1)
    return curvature.max() < 0.3

def min_crop(lines):
    box = Crop(
        min([line.left() for line in lines]),
        min([letter.y for letter in lines[0]]),
        max([line.right() for line in lines]),
        max([letter.y + letter.h for letter in lines[-1]]),
    )
    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    box.draw(debug)
    lib.debug_imwrite('crop.png', debug)
    return box

@lib.timeit
def dewarp_fine(im):
    lib.debug_prefix = 'fine_'

    AH, all_lines, lines = get_AH_lines(im)

    points = []
    offsets = []
    for line in lines:
        bases = np.array([l.base_point() for l in line])
        median_y = np.median(bases[:, 1])
        points.extend(bases)
        offsets.extend(median_y - bases[:, 1])

    points = np.array(points)
    offsets = np.array(offsets)

    im_h, im_w = im.shape
    # grid_x, grid_y = np.mgrid[:im_h, :im_w]
    # y_offset_interp = interpolate.griddata(points, offsets,
    #                                        (grid_x, grid_y), method='nearest')
    y_offset_interp = interpolate.SmoothBivariateSpline(
        points[:, 0], points[:, 1], offsets
    )

    new = np.full(im.shape, 0, dtype=np.uint8)
    _, contours, [hierarchy] = \
        cv2.findContours(im, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    draw_contours(new, contours, hierarchy, y_offset_interp, 0, 255)

    lib.debug_imwrite('fine.png', new)

    return new

def draw_contours(im, contours, hierarchy, y_offset_interp, idx, color,
                  depth=0, passed_offset=None):
    while idx >= 0:
        x, y, w, h = cv2.boundingRect(contours[idx])
        # print '+' * depth, idx, 'color:', color, '@', x, y, 'offset:', offset, 'area:', w * h
        if passed_offset is None:
            offset = (0, -int(round(y_offset_interp(x + w / 2.0, y + h))))
            # offset = (0, -int(round(y_offset_interp[y + h - 1, x + w / 2 - 1])))
        else:
            offset = passed_offset

        cv2.drawContours(im, contours, idx, color, thickness=cv2.FILLED,
                         offset=offset)
        child = hierarchy[idx][2]
        if child >= 0:
            pass_offset = offset if color == 0 and w * h < 5000 else None
            draw_contours(im, contours, hierarchy, y_offset_interp, child,
                          255 - color, depth=depth + 1, passed_offset=pass_offset)
        idx = hierarchy[idx][0]

def full_lines(AH, lines, v):
    C0 = max(lines, key=lambda l: l.right() - l.left())

    v_lefts = [Line.from_points(v, l[0].left_bot()) for l in lines if l is not C0]
    v_rights = [Line.from_points(v, l[-1].right_bot()) for l in lines if l is not C0]
    C0_lefts = [l.text_line_intersect(C0)[0] for l in v_lefts]
    C0_rights = [l.text_line_intersect(C0)[0] for l in v_rights]

    mask = np.logical_and(C0_lefts <= C0.left() + AH, C0_rights >= C0.right() - AH)
    return compress(lines, mask)

def get_AH_lines(im):
    all_letters = algorithm.all_letters(im)
    AH = algorithm.dominant_char_height(im, letters=all_letters)
    print('AH =', AH)
    letters = algorithm.letter_contours(AH, im, letters=all_letters)
    print('collating...')
    all_lines = lib.timeit(collate.collate_lines)(AH, letters)
    all_lines.sort(key=lambda l: l[0].y)

    print('combining...')
    combined = algorithm.combine_underlined(AH, im, all_lines, all_letters)

    print('removing stroke outliers...')
    filtered = algorithm.remove_stroke_outliers(bw, combined, k=2.0)

    lines = remove_outliers(im, AH, filtered)
    # lines = combined

    # if lib.debug:
    #     debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    #     for l in all_lines:
    #         for l1, l2 in zip(l, l[1:]):
    #             cv2.line(debug, tuple(l1.base_point().astype(int)),
    #                     tuple(l2.base_point().astype(int)), RED, 2)
    #     lib.debug_imwrite('all_lines.png', debug)

    return AH, combined, lines

N_LONGS = 15
def vanishing_point(lines, v0, O):
    C0 = lines[-1] if v0[1] < 0 else lines[0]
    others = lines[:-1] if v0[1] < 0 else lines[1:]

    domain = np.linspace(C0.left(), C0.right(), N_LONGS + 2)[1:-1]
    C0_points = np.array([domain, C0.model(domain)]).T
    longitudes = [Line.from_points(v0, p) for p in C0_points]

    lefts = [longitudes[0].text_line_intersect(line)[0] for line in others]
    rights = [longitudes[-1].text_line_intersect(line)[0] for line in others]
    valid_mask = [line.left() <= L and R < line.right() \
                   for line, L, R in zip(others, lefts, rights)]

    valid_lines = [C0] + compress(others, valid_mask)
    derivs = [line.model.deriv() for line in valid_lines]
    print('valid lines:', len(others))

    convergences = []
    for longitude in longitudes:
        intersects = [longitude.text_line_intersect(line) for line in valid_lines]
        tangents = [Line.from_point_slope(p, d(p[0])) \
                    for p, d in zip(intersects, derivs)]
        convergences.append(Line.best_intersection(tangents))

    # x vx + y vy + f^2 = 0
    # m = -vx / vy
    # b = -f^2 / vy


    L = Line.fit(convergences)
    # shift into O-origin coords
    L_O = L.offset(-O)
    vy = -(f ** 2) / L_O.b
    vx = -vy * L_O.m
    v = np.array((vx, vy)) + O

    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for t in tangents: t.draw(debug, color=RED)
    for longitude in longitudes:
        longitude.draw(debug)
    L.draw(debug, color=GREEN)
    lib.debug_imwrite('vanish.png', debug)

    return v, f, L

def dewarp(orig):
    # Meng et al., Metric Rectification of Curved Document Images
    lib.debug = True
    im = binarize.binarize(orig, algorithm=binarize.ntirogiannis2014)
    global bw
    bw = im
    im_h, im_w = im.shape

    AH, all_lines, lines = get_AH_lines(im)

    v0 = estimate_vanishing(AH, lines)

    O = np.array((im_w / 2.0, im_h / 2.0))
    v = v0
    print('vanishing point:', v)
    for i in range(5):
        v, L = vanishing_point(lines, v, O)
        print('vanishing point:', v)

    lines = full_lines(AH, lines, v)

    box = min_crop(all_lines)
    D, C_arc = estimate_directrix(lines, v, box.w)

    r = aspect_ratio(im, lines, D, v, O)
    print('aspect ratio H/W:', r)
    print('fixing to 1.7')
    r = 1.7  # TODO: fix

    print('generating mesh...')
    mesh = generate_mesh(all_lines, lines, C_arc, v, r * box.w)

    print('dewarping...')
    dewarped = correct_geometry(orig, mesh)

    # print 'binarizing...'
    # dewarped_bw = binarize.binarize(dewarped, algorithm=lambda im: binarize.yan(im, alpha=0.3))

    # print 'fine dewarping...'
    # fine = dewarp_fine(dewarped_bw)

    return dewarped

# rotation matrix for rotation by ||theta|| around axis theta
# theta: 3component x N; return: 3 x 3matrix x N
def R_theta(theta):
    # these are all N-vectors
    T = norm(theta, axis=0)
    t1, t2, t3 = theta / T
    c, s = np.cos(T / 2), np.sin(T / 2)
    ss = s * s
    cs = c * s

    return np.array([
        [2 * (t1 * t1 - 1) * ss + 1,
         2 * t1 * t2 * ss - 2 * t3 * cs,
         2 * t1 * t3 * ss + 2 * t2 * cs],
        [2 * t1 * t2 * ss + 2 * t3 * cs,
         2 * (t2 * t2 - 1) * ss + 1,
         2 * t2 * t3 * ss - 2 * t1 * cs],
        [2 * t1 * t2 * ss - 2 * t2 * cs,
         2 * t2 * t3 * ss + 2 * t1 * cs,
         2 * (t3 * t3 - 1) * ss + 1]
    ])

FOCAL_PLANE_Z = -f
# T0 = 0.6 * FOCAL_PLANE_Z / f
# T0 = -0.7
def image_to_focal_plane(points, O):
    if type(points) != np.ndarray:
        points = np.array(points)

    assert points.shape[0] == 2
    return np.concatenate((
        points - O[:, newaxis],
        np.full(points.shape[1:], FOCAL_PLANE_Z)[newaxis, ...]
    )).astype(np.float64)

# points: 3 x ... array of points
def project_to_image(points, O):
    assert points.shape[0] == 3
    projected = (points * FOCAL_PLANE_Z / points[2])[0:2]
    return (projected.T + O).T

# points: 3 x ... array of points
def gcs_to_image(points, O, R):
    # invert R(pt - Of)
    assert points.shape[0] == 3
    image_coords = np.tensordot(inv(R), points, axes=1)
    image_coords_T = image_coords.T
    image_coords_T += Of
    return project_to_image(image_coords, O)

# O: two-dimensional origin (middle of image/principal point)
# returns points on focal plane
def line_base_points_modeled(line, O):
    model = line.fit_poly()
    x0, _ = line[0].base_point() + 5
    x1, _ = line[-1].base_point() - 5
    domain = np.linspace(x0, x1, len(line))
    points = np.stack([domain, model(domain)])
    return image_to_focal_plane(points, O)

def line_base_points(line, O):
    return image_to_focal_plane(line.base_points().T, O)

# represents g(x) = 1/w h(wx)
class NormPoly(object):
    def __init__(self, coef, omega):
        self.h = Poly(coef)
        self.omega = omega

    def __call__(self, x):
        return self.h(self.omega * x) / self.omega

    def deriv(self):
        return NormPoly(self.omega * self.h.deriv().coef, self.omega)

    def degree(self):
        return self.h.degree()

    @property
    def coef(self):
        return self.h.coef

def split_lengths(array, lengths):
    return np.split(array, np.cumsum(lengths))

DEGREE = 5
OMEGA = 1000.
def unpack_args(args, n_pages):
    # theta: 3; a_m: DEGREE; align: 2; l_m: len(lines)
    theta, a_m_all, align_all, l_m_all = \
        split_lengths(np.array(args), (3, DEGREE * n_pages, 2 * n_pages))

    a_ms = np.split(a_m_all, n_pages)
    aligns = np.split(align_all, n_pages)

    gs = [NormPoly(np.concatenate([[0], a_m]), OMEGA) for a_m in a_ms]
    assert all((g.degree() == DEGREE for g in gs))

    return theta, a_ms, aligns, l_m_all, gs

E_str_t0s = []
def E_str_project(R, g, base_points, t0s_idx):
    global E_str_t0s
    if len(E_str_t0s) <= t0s_idx:
        E_str_t0s.extend([None] * (t0s_idx - len(E_str_t0s) + 1))
    if E_str_t0s[t0s_idx] is None:
        E_str_t0s[t0s_idx] = \
            [np.full((points.shape[1],), np.inf) for points in base_points]

    # print([point.shape for point in base_points])
    # print([t0s.shape for t0s in E_str_t0s])

    return [newton.t_i_k(R, g, points, t0s) \
            for points, t0s in zip(base_points, E_str_t0s[t0s_idx])]

# l_m = fake parameter representing line position
# base_points = text base points on focal plane
def E_str(theta, g, l_m, base_points, page_idx):
    assert len(base_points) == l_m.shape[0]

    # print '    theta:', theta
    # print '    a_m:', g.coef
    R = R_theta(theta)
    all_ts_surface = E_str_project(R, g, base_points, page_idx)

    residuals = []
    for ts_surface, l_k in zip(all_ts_surface, l_m):
        _, (_, Ys, _) = ts_surface
        # print('ts:', ts.min(), np.median(ts), ts.max())
        # print('Zs:', Zs.min(), np.median(Zs), Zs.max())
        residuals.append(Ys - l_k)

    result = np.concatenate(residuals)
    return result

def E_str_packed(args, base_points):
    theta, _, _, l_m_all, gs = unpack_args(args, len(base_points))
    l_ms = split_lengths(l_m_all, [len(points) for points in base_points[:-1]])

    blocks = []
    for i, (g, l_m, points) in enumerate(zip(gs, l_ms, base_points)):
        blocks.append(E_str(theta, g, l_m, points, i))

    return np.concatenate(blocks)

def E_0(args, base_points):
    result = E_str_packed(args, base_points)
    print('norm:', norm(result))
    return result

def dR_dthetai(theta, R, i):
    T = norm(theta)
    inc = T / 8192
    delta = np.zeros(3)
    delta[i] = inc
    Rp = R_theta(theta + delta)
    Rm = R_theta(theta - delta)
    return (Rp - Rm) / (2 * inc)

def dR_dtheta(theta, R):
    return np.array([dR_dthetai(theta, R, i) for i in range(3)])

def dti_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface):
    R1, _, R3 = R
    dR1, dR3 = dR[:, 0], dR[:, 2]
    dR13, dR33 = dR[:, 0, 2], dR[:, 2, 2]

    Xs, _, _ = all_surface

    # dR: 3derivs x r__; dR[:, 0]: 3derivs x r1_; points: 3comps x Npoints
    # A: 3 x Npoints
    A1 = dR1.dot(all_points) * all_ts
    A2 = -dR13 * f
    A = A1 + A2[:, newaxis]
    B = R1.dot(all_points)
    C1 = dR3.dot(all_points) * all_ts  # 3derivs x Npoints
    C2 = -dR33 * f
    C = C1 + C2[:, newaxis]
    D = R3.dot(all_points)
    slopes = gp(Xs)
    return -(C - slopes * A) / (D - slopes * B)

def dE_str_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface):
    _, R2, _ = R
    dR2 = dR[:, 1]
    dR23 = dR[:, 1, 2]

    dt = dti_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface)

    term1 = dR2.dot(all_points) * all_ts
    term2 = R2.dot(all_points) * dt
    term3 = -dR23 * f

    return term1.T + term2.T + term3

def dti_dam(theta, R, g, gp, all_points, all_ts, all_surface):
    R1, R2, R3 = R

    Xs, _, _ = all_surface

    powers = np.vstack([Xs ** m * g.omega ** (m - 1) for m in range(1, DEGREE + 1)])
    denom = R3.dot(all_points) - gp(Xs) * R1.dot(all_points)

    return powers / denom

def dE_str_dam(theta, R, g, gp, all_points, all_ts, all_surface):
    R1, R2, R3 = R

    dt = dti_dam(theta, R, g, gp, all_points, all_ts, all_surface)

    return (R2.dot(all_points) * dt).T

def dE_str_dl_k(base_points):
    blocks = [np.full((l.shape[-1], 1), -1) for l in base_points]
    return scipy.linalg.block_diag(*blocks)

def debug_plot_g(g, line_ts_surface):
    import matplotlib.pyplot as plt
    all_points_XYZ = np.concatenate([points for _, points in line_ts_surface],
                                    axis=1)
    domain = np.linspace(all_points_XYZ[0].min(), all_points_XYZ[0].max(), 100)
    plt.plot(domain, g(domain))
    # domain = np.linspace(-im_w / 2, im_w / 2, 100)
    # plt.plot(domain, g(domain))
    plt.show()

def Jac_E_str(args, base_points):
    theta, _, align, l_m_all, gs = unpack_args(args, len(base_points))
    R = R_theta(theta)
    dR = dR_dtheta(theta, R)

    theta_blocks = []
    a_m_blocks = []
    align_blocks = []
    l_k_blocks = []
    for i, (g, page_bases) in enumerate(zip(gs, base_points)):
        gp = g.deriv()

        line_ts_surface = E_str_project(R, g, page_bases, i)

        all_points = np.concatenate(page_bases, axis=1)
        all_ts = np.concatenate([ts for ts, _ in line_ts_surface])
        all_surface = np.concatenate([surface for _, surface in line_ts_surface],
                                     axis=1)
        theta_blocks.append(
            dE_str_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface)
        )

        a_m_blocks.append(
            dE_str_dam(theta, R, g, gp, all_points, all_ts, all_surface)
        )

        align_blocks.append(np.zeros((all_ts.shape[0], 2),
                                     dtype=np.float64))

        l_k_blocks.append(dE_str_dl_k(page_bases))

    return np.concatenate((
        np.concatenate(theta_blocks),
        block_diag(*a_m_blocks),
        block_diag(*align_blocks),
        block_diag(*l_k_blocks),
    ), axis=1)

def debug_jac(theta, R, g, l_m, base_points, line_ts_surface):
    dR = dR_dtheta(theta, R)
    gp = g.deriv()

    all_points = np.concatenate(base_points, axis=1)
    all_ts = np.concatenate([ts for ts, _ in line_ts_surface])
    all_surface = np.concatenate([surface for _, surface in line_ts_surface], axis=1)

    print('dE_str_dtheta')
    print(dE_str_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface).T)
    for i in range(3):
        delta = np.zeros(3)
        inc = norm(theta) / 4096
        delta[i] = inc
        diff = E_str(theta + delta, g, l_m, base_points) - E_str(theta - delta, g, l_m, base_points)
        print(diff / (2 * inc))

    if not np.all(g.coef == 0.):
        print('dE_str_dam')
        print(dE_str_dam(theta, R, g, gp, all_points, all_ts, all_surface).T)
        for i in range(1, DEGREE + 1):
            delta = np.zeros(DEGREE + 1)
            inc = g.coef[i] / 4096
            delta[i] = inc
            diff = E_str(theta, NormPoly(g.coef + delta, g.omega), l_m, base_points) \
                - E_str(theta, NormPoly(g.coef - delta, g.omega), l_m, base_points)
            print(diff / (2 * inc))

def side_slice(left, right):
    assert left or right

    if left and right:
        return np.s_[:]
    elif left:
        return np.s_[:1]
    else:
        return np.s_[1:]

E_align_t0s = []
def E_align_project(R, g, all_points, t0s_idx):
    global E_align_t0s
    if len(E_align_t0s) <= t0s_idx:
        E_align_t0s.extend([None] * (t0s_idx - len(E_align_t0s) + 1))
    if E_align_t0s[t0s_idx] is None:
        E_align_t0s[t0s_idx] = np.full((all_points.shape[1],), np.inf)

    return newton.t_i_k(R, g, all_points, E_align_t0s[t0s_idx])

def E_align(theta, g, align, side_points, left, right):
    R = R_theta(theta)

    all_points = side_points.reshape(3, -1)

    _, (Xs, _, _) = E_align_project(R, g, all_points)
    Xs.shape = (int(left) + int(right), -1)  # 2 x N

    return (Xs - align[side_slice(left, right), newaxis]).flatten()

def E_align_packed(args, all_side_points, left, right):
    theta, _, aligns, _, gs = unpack_args(args, len(all_side_points))
    blocks = []
    for g, align, page_sides in zip(gs, aligns, all_side_points):
        side_points = all_side_points[:, side_slice(left, right), :]
        blocks.append(E_align(theta, g, align, side_points, left, right))

    return np.concatenate(blocks)

def dE_align_dam(theta, R, g, gp, all_points, all_ts, all_surface):
    R1, _, _ = R

    dt = dti_dam(theta, R, g, gp, all_points, all_ts, all_surface)

    return (R1.dot(all_points) * dt).T

def dE_align_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface):
    R1, _, _ = R
    dR1 = dR[:, 0]
    dR13 = dR[:, 0, 2]

    dt = dti_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface)

    term1 = dR1.dot(all_points) * all_ts
    term2 = R1.dot(all_points) * dt
    term3 = -dR13 * f

    return term1.T + term2.T + term3

def Jac_E_align(args, all_side_points, base_points, left, right):
    theta, a_m, _, _, g = unpack_args(args, len(base_points))
    R = R_theta(theta)
    dR = dR_dtheta(theta, R)
    gp = g.deriv()

    side_points = all_side_points[:, side_slice(left, right), :]
    N = side_points.shape[-1]  # number of lines; 2N residuals
    n_align = int(left) + int(right)

    all_points = side_points.reshape(3, n_align * N)
    all_ts, all_surface= E_align_project(R, g, all_points)

    align_jac = []
    if left:
        align_jac.append([-1, 0])
    if right:
        align_jac.append([0, -1])

    # print(dE_align_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface).shape)
    # print(dE_align_dam(theta, R, g, gp, all_points, all_ts, all_surface).shape)
    # print(np.tile(align_jac, (N, 1)).shape)
    # print(np.zeros((n_align * N, len(base_points))).shape)
    return np.concatenate((
        dE_align_dtheta(theta, R, dR, g, gp, all_points, all_ts, all_surface),
        dE_align_dam(theta, R, g, gp, all_points, all_ts, all_surface),
        np.tile(align_jac, (N, 1)),
        np.zeros((n_align * N, len(base_points)))
    ), axis=1)

LAMBDA_2 = 0.1
def E_2(args, side_points, base_points, left, right):
    E_str_out = E_str_packed(args, base_points)
    E_align_out = LAMBDA_2 * E_align_packed(args, side_points, left, right)
    result = np.concatenate([E_str_out, E_align_out])
    print('norm:', norm(result), '=', norm(E_str_out), '+', norm(E_align_out))
    return result

def Jac_E_2(args, side_points, base_points, left, right):
    return np.concatenate([
        Jac_E_str(args, base_points),
        LAMBDA_2 * Jac_E_align(args, side_points, base_points, left, right)
    ])

def make_mesh_XYZ(xs, ys, g):
    return np.array([
        np.tile(xs, [len(ys), 1]),
        np.tile(ys, [len(xs), 1]).T,
        np.tile(g(xs), [len(ys), 1])
    ])

def normalize_theta(theta):
    angle = norm(theta)
    quot = int(angle / (2 * pi))
    mod = angle - 2 * pi * quot
    return theta * (mod / angle)

def debug_print_points(filename, points, step=None, color=BLUE):
    if lib.debug:
        debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
        if step is not None:
            points = points[[np.s_[:]] + [np.s_[::step]] * (points.ndim - 1)]
        for p in points.reshape(2, -1).T:
            draw_circle(debug, p, color=color)
        lib.debug_imwrite(filename, debug)

@lib.timeit
def make_mesh_2d(all_lines, O, R, g):
    all_letters = np.concatenate([line.letters for line in all_lines])
    corners_2d = np.concatenate([letter.corners() for letter in all_letters]).T
    corners = image_to_focal_plane(corners_2d, O)
    t0s = np.full((corners.shape[1],), np.inf, dtype=np.float64)
    _, corners_XYZ = newton.t_i_k(R, g, corners, t0s)

    corners_X, _, corners_Z = corners_XYZ
    relative_Z_error = (g(corners_X) - corners_Z) / corners_Z
    corners_XYZ = corners_XYZ[:, relative_Z_error <= 0.02]

    debug_print_points('corners.png', corners_2d)

    box_XYZ = Crop.from_points(corners_XYZ[:2]).expand(0.01)
    print('box_XYZ:', box_XYZ)

    # 70th percentile line width a good guess
    n_points_w = 1.2 * np.percentile(np.array([line.width() for line in all_lines]), 90)
    mesh_XYZ_x = np.linspace(box_XYZ.x0, box_XYZ.x1, 400)
    mesh_XYZ_z = g(mesh_XYZ_x)
    mesh_XYZ_xz_arc, total_arc = arc_length_points(mesh_XYZ_x, mesh_XYZ_z,
                                                   n_points_w)
    mesh_XYZ_x_arc, _ = mesh_XYZ_xz_arc

    # TODO: think more about estimation of aspect ratio for mesh
    n_points_h = n_points_w * box_XYZ.h / total_arc
    # n_points_h = n_points_w * 1.7

    mesh_XYZ_y = np.linspace(box_XYZ.y0, box_XYZ.y1, n_points_h)
    mesh_XYZ = make_mesh_XYZ(mesh_XYZ_x_arc, mesh_XYZ_y, g)
    mesh_2d = gcs_to_image(mesh_XYZ, O, R)
    print('mesh:', Crop.from_points(mesh_2d))

    debug_print_points('mesh1.png', mesh_2d, step=20)

    # make sure meshes are not reversed
    if mesh_2d[0, :, 0].mean() > mesh_2d[0, :, -1].mean():
        mesh_2d = mesh_2d[:, :, ::-1]

    if mesh_2d[1, 0].mean() > mesh_2d[1, -1].mean():
        mesh_2d = mesh_2d[:, ::-1, :]

    return mesh_2d.transpose(1, 2, 0)

def Jac_to_grad_lsq(residuals, jac, args):
    jacobian = jac(*args)
    return residuals.dot(jacobian)

def lsq(func, jac):
    def result(*args):
        residuals = func(*args)
        return np.dot(residuals, residuals), Jac_to_grad_lsq(residuals, jac, args)

    return result

def lm(fun, x0, jac, args=(), kwargs={}, ftol=1e-6, max_nfev=10000, x_scale=None):
    LAM_UP = 1.2
    LAM_DOWN = 4.0

    if x_scale is None:
        x_scale = np.ones(x0.shape[0], dtype=np.float64)

    x = x0
    xs = x / x_scale
    lam = 100.

    r = fun(x, *args, **kwargs)
    C = dot(r, r) / 2
    Js = jac(x, *args, **kwargs) * x_scale[newaxis, :]
    dC = dot(Js.T, r)
    JsTJs = dot(Js.T, Js)
    assert r.shape[0] == Js.shape[0]

    I = np.eye(Js.shape[1])

    for _ in range(max_nfev):
        xs_new = xs - solve(JsTJs + lam * I, dC)
        x_new = xs_new * x_scale

        r_new = fun(x_new, *args, **kwargs)
        C_new = dot(r_new, r_new) / 2
        # print('trying step: size {:.3g}, C {:.3g}, lam {:.3g}'.format(
        #     norm(x - x_new), C_new, lam
        # ))
        # print(x - x_new)
        if C_new >= C:
            lam *= LAM_UP
            if lam >= 1000: break
            continue

        relative_err = abs(C - C_new) / C
        if relative_err <= ftol:
            break

        xs = xs_new
        x = xs * x_scale
        r = r_new

        C = C_new

        if C < 1e-6: break

        Js = jac(x, *args, **kwargs) * x_scale[newaxis, :]
        dC = dot(Js.T, r)
        JsTJs = dot(Js.T, Js)
        lam /= LAM_DOWN

    return x

def initial_args(line_groups, O, AH):
    # Estimate viewpoint from vanishing point
    vx, vy = np.mean([estimate_vanishing(AH, lines) for lines in line_groups]) - O

    theta_0 = [atan2(-vy, f) - pi / 2, 0, 0]
    print('theta_0:', theta_0)

    # flat surface as initial guess.
    # NB: coeff 0 forced to 0 here. not included in opt.
    a_m_0 = [0] * (DEGREE * len(line_groups))

    R_0 = R_theta(theta_0)
    _, ROf_y, ROf_z = R_0.dot(Of)

    # line points on focal plane
    base_points = [[line_base_points(line, O) for line in lines]
                   for lines in line_groups]

    debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for lines in base_points:
        for line in lines:
            for p in line.T:
                draw_circle(debug, project_to_image(p, O), color=GREEN)

    # make underlines straight as well
    for lines, bases in zip(line_groups, base_points):
        underlines = sum([line.underlines for line in lines], [])
        print('underlines:', len(underlines))
        for underline in underlines:
            mid_contour = (underline.top_contour() + underline.bottom_contour()) / 2
            all_mid_points = np.stack([
                underline.x + np.arange(underline.w), mid_contour,
            ])
            mid_points = all_mid_points[:, ::4]
            for p1, p2 in zip(mid_points.T, mid_points.T[1:]):
                draw_line(debug, p1, p2, color=lib.BLUE)

            bases.append(image_to_focal_plane(mid_points, O))

    lib.debug_imwrite('opt_points.png', debug)

    # line left-mid and right-mid points on focal plane.
    # axes after transpose: (coord 2, LR 2, line N)
    side_points_2d = [np.array([
        [line[0].left_mid() for line in lines],
        [line[-1].right_mid() for line in lines],
    ]).transpose(2, 0, 1) for lines in line_groups]
    # widths = abs(side_points_2d[0, 1] - side_points_2d[0, 0])
    # side_points_2d = side_points_2d[:, :, widths >= 0.9 * np.median(widths)]
    assert side_points_2d[0].shape[0:2] == (2, 2)

    # axes (coord 3, LR 2, line N)
    side_points = [image_to_focal_plane(points, O) for points in side_points_2d]
    assert side_points[0].shape[0:2] == (3, 2)

    all_surface = [[R_0.dot(-points - Of[:, newaxis]) for points in bases]
                   for bases in base_points]
    l_m_0s = [[Ys.mean() for _, Ys, _ in page_surface]
              for page_surface in all_surface]

    align_0s = []
    for page_sides in side_points:
        Xs, _, _ = R_0.dot(-page_sides.reshape(3, -1) - Of[:, newaxis])
        align_0 = Xs.reshape(2, -1).mean(axis=1)  # to LR, N
        print('align_0:', align_0)
        align_0s.append(align_0)

    return (np.concatenate([theta_0, a_m_0] + align_0s + l_m_0s),
            (side_points, base_points))

def kim2014(orig):
    lib.debug_prefix = 'dewarp/'

    im = binarize.binarize(orig, algorithm=lambda im: binarize.sauvola(im, k=0.1))
    global bw
    bw = im

    im_h, im_w = im.shape

    AH, all_lines, lines = get_AH_lines(im)

    lines = crop.filter_position(AH, im, lines, im_w > im_h)

    O = np.array((im_w / 2.0, im_h / 2.0))

    # Test if line start distribution is bimodal.
    line_xs = np.array([line.left() for line in lines])
    bimodal = line_xs.std() / im_w > 0.10

    if bimodal and im_w > im_h:
        print('Bimodal! Splitting page!')
        line_groups, all_line_groups = crop.split_lines(lines, all_lines=all_lines)
    else:
        line_groups = [lines]
        all_line_groups = [all_lines]

    for lines in line_groups:
        lines.sort(key=lambda line: line[0].y)

    global E_str_t0s, E_align_t0s
    E_str_t0s, E_align_t0s = [], []

    args_0, (side_points, base_points) = initial_args(line_groups, O, AH)

    # x_scale = np.concatenate([
    #     [0.2] * 3,
    #     1000 * (1e-6 ** np.arange(DEGREE)),
    #     [1000, 1000],
    #     [500] * len(base_points),
    # ])

    result = opt.least_squares(
    # result = lm(
        fun=E_0,
        x0=args_0,
        jac=Jac_E_str,
        # method='lm',
        args=(base_points,),
        # ftol=1e-3,
        # max_nfev=4000,
        x_scale='jac',
        # x_scale=x_scale,
    )

    # theta, a_m, align, l_m, g = unpack_args(result)
    # final_norm = norm(E_0(result, base_points))
    theta, a_ms, aligns, l_m_all, gs = unpack_args(result.x, len(base_points))
    l_ms = split_lengths(l_m_all, [len(points) for points in base_points[:-1]])
    final_norm = norm(result.fun)

    print('*** DONE ***')
    print('final norm:', final_norm)
    print('theta:', theta)
    for a_m in a_ms:
        print('a_m:', np.concatenate([[0], a_m]))
    # print('l_m:', l_m)

    R = R_theta(theta)

    debug = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    for i, (g, l_m, page_bases) in enumerate(zip(gs, l_ms, base_points)):
        ts_surface = E_str_project(R, g, page_bases, i)
        for Y, (_, points_XYZ) in zip(l_m, ts_surface):
            Xs, Ys, _ = points_XYZ
            # print('Y diffs:', Ys - Y)
            X_min, X_max = Xs.min(), Xs.max()
            line_Xs = np.linspace(X_min, X_max, 100)
            line_Ys = np.full((100,), Y)
            line_Zs = g(line_Xs)
            line_XYZ = np.stack([line_Xs, line_Ys, line_Zs])
            line_2d = gcs_to_image(line_XYZ, O, R).T
            for p0, p1 in zip(line_2d, line_2d[1:]):
                draw_line(debug, p0, p1, GREEN, 1)

    lib.debug_imwrite('surface_lines.png', debug)

    for all_lines, g in zip(all_line_groups, gs):
        mesh_2d = make_mesh_2d(all_lines, O, R, g)
        first_pass = correct_geometry(orig, mesh_2d, interpolation=cv2.INTER_LANCZOS4)
        yield first_pass

    # if lib.debug:
    #     mesh_2d = make_mesh_2d(all_lines, O, R, g)
    #     correct_geometry(orig, mesh_2d)

    # lib.debug_prefix = 'dewarp2/'

    # im = binarize.binarize(first_pass, algorithm=binarize.ntirogiannis2014)
    # bw = im

    # # find nearest point in new image to original principal point
    # O_distance = norm(mesh_2d - O, axis=2)
    # O = np.array(np.unravel_index(O_distance.argmin(), O_distance.shape))

    # AH, all_lines, lines = get_AH_lines(im)

    # args_0, (side_points, base_points) = initial_args(lines, O)
    # _, (Xs, _, _) = newton.t_i_k(R, g, side_points.reshape(3, -1), T0)
    # align = Xs.reshape(2, -1).mean(axis=1)  # to LR, N

    # # TODO: use E_1 if not aligned
    # result = lib.timeit(opt.least_squares)(
    #     fun=E_2,
    #     x0=np.concatenate([theta, a_m, align, l_m]),
    #     jac=Jac_E_2,
    #     method='lm',
    #     args=(side_points, base_points, True, False),
    #     ftol=1e-4,
    #     x_scale='jac',
    # )
    # theta, a_m, align, l_m, g = unpack_args(result.x)
    # print('*** DONE ***')
    # print('final norm:', norm(result.fun))
    # print('theta:', theta)
    # print('a_m:', np.hstack([[0], a_m]))
    # # print('l_m:', l_m)
    # # print('alignment:', (result.fun[-2 * side_points.shape[-1]:] / LAMBDA_2).astype(int))

    # R = R_theta(theta)

    # debug = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    # for idx, X in enumerate(align):
    #     line = Line3D.from_point_vec((X, 0, g(X)), (0, 1, 0))
    #     line.transform(inv(R)).offset(Of).project(FOCAL_PLANE_Z)\
    #         .offset(-O).draw(debug, color=BLUE if idx == 0 else GREEN)
    # lib.debug_imwrite('align.png', debug)

    # bw = binarize.binarize(second_pass, algorithm=binarize.sauvola)
    # AH, all_lines, lines = get_AH_lines(bw)
    # algorithm.fine_dewarp(binarize.grayscale(second_pass), lines)

def go(argv):
    im = cv2.imread(argv[1], cv2.IMREAD_UNCHANGED)
    lib.debug = True
    out = kim2014(im)
    for i, outimg in enumerate(out):
        gray = binarize.grayscale(outimg).astype(np.float64)
        gray -= np.percentile(gray, 2)
        gray *= 255 / np.percentile(gray, 95)
        norm = binarize.ng2014_normalize(lib.clip_u8(gray))
        cv2.imwrite('dewarped{}.png'.format(i), norm)

if __name__ == '__main__':
    go(sys.argv)
