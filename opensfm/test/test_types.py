import numpy as np
import cv2
import math

from opensfm import context
from opensfm import pygeometry
from opensfm import pymap
from opensfm import types

from scipy.stats import special_ortho_group
from scipy.spatial.transform import Rotation


"""
Trying to imitate the following structure

reconstruction = {
    "cameras": {
        "theta": {
            "projection_type": "equirectangular"
        }
    },

    "shots" : {
        'im1': {
            "camera": "theta",
            "rotation": [0.0, 0.0, 0.0],
            "translation": [0.0, 0.0, 0.0],
        },
        'im2': {
            "camera": "theta",
            "rotation": [0, 0, 0.0],
            "translation": [-1, 0, 0.0],
        },
    },

    "points" : {
    },
}
"""
# Define original Pose
class Pose(object):
    """Defines the pose parameters of a camera.

    The extrinsic parameters are defined by a 3x1 rotation vector which
    maps the camera rotation respect to the origin frame (rotation) and
    a 3x1 translation vector which maps the camera translation respect
    to the origin frame (translation).

    Attributes:
        rotation (vector): the rotation vector.
        translation (vector): the rotation vector.
    """

    def __init__(self, rotation=np.zeros(3), translation=np.zeros(3)):
        self.rotation = rotation
        self.translation = translation

    @property
    def rotation(self):
        """Rotation in angle-axis format."""
        return self._rotation

    @rotation.setter
    def rotation(self, value):
        self._rotation = np.asarray(value, dtype=float)

    @property
    def translation(self):
        """Translation vector."""
        return self._translation

    @translation.setter
    def translation(self, value):
        self._translation = np.asarray(value, dtype=float)

    def transform(self, point):
        """Transform a point from world to this pose coordinates."""
        return self.get_rotation_matrix().dot(point) + self.translation

    def transform_many(self, points):
        """Transform points from world coordinates to this pose."""
        return points.dot(self.get_rotation_matrix().T) + self.translation

    def transform_inverse(self, point):
        """Transform a point from this pose to world coordinates."""
        return self.get_rotation_matrix().T.dot(point - self.translation)

    def transform_inverse_many(self, points):
        """Transform points from this pose to world coordinates."""
        return (points - self.translation).dot(self.get_rotation_matrix())

    def get_rotation_matrix(self):
        """Get rotation as a 3x3 matrix."""
        return cv2.Rodrigues(self.rotation)[0]

    def set_rotation_matrix(self, rotation_matrix, permissive=False):
        """Set rotation as a 3x3 matrix.

        >>> pose = Pose()
        >>> pose.rotation = np.array([0., 1., 2.])
        >>> R = pose.get_rotation_matrix()
        >>> pose.set_rotation_matrix(R)
        >>> np.allclose(pose.rotation, [0., 1., 2.])
        True

        >>> pose.set_rotation_matrix([[3,-4, 1], [ 5, 3,-7], [-9, 2, 6]])
        Traceback (most recent call last):
        ...
        ValueError: Not orthogonal

        >>> pose.set_rotation_matrix([[0, 0, 1], [-1, 0, 0], [0, 1, 0]])
        Traceback (most recent call last):
        ...
        ValueError: Determinant not 1
        """
        R = np.array(rotation_matrix, dtype=float)
        if not permissive:
          if not np.isclose(np.linalg.det(R), 1):
              raise ValueError("Determinant not 1")
          if not np.allclose(np.linalg.inv(R), R.T):
              raise ValueError("Not orthogonal")
        self.rotation = cv2.Rodrigues(R)[0].ravel()

    def get_origin(self):
        """The origin of the pose in world coordinates."""
        return -self.get_rotation_matrix().T.dot(self.translation)

    def set_origin(self, origin):
        """Set the origin of the pose in world coordinates.

        >>> pose = Pose()
        >>> pose.rotation = np.array([0., 1., 2.])
        >>> origin = [1., 2., 3.]
        >>> pose.set_origin(origin)
        >>> np.allclose(origin, pose.get_origin())
        True
        """
        self.translation = -self.get_rotation_matrix().dot(origin)

    def get_Rt(self):
        """Get pose as a 3x4 matrix (R|t)."""
        Rt = np.empty((3, 4))
        Rt[:, :3] = self.get_rotation_matrix()
        Rt[:, 3] = self.translation
        return Rt

    def compose(self, other):
        """Get the composition of this pose with another.

        composed = self * other
        """
        selfR = self.get_rotation_matrix()
        otherR = other.get_rotation_matrix()
        R = np.dot(selfR, otherR)
        t = selfR.dot(other.translation) + self.translation
        res = Pose()
        res.set_rotation_matrix(R)
        res.translation = t
        return res

    def inverse(self):
        """Get the inverse of this pose."""
        inverse = Pose()
        R = self.get_rotation_matrix()
        inverse.set_rotation_matrix(R.T)
        inverse.translation = -R.T.dot(self.translation)
        return inverse

class Camera(object):
    """Abstract camera class.

    A camera is unique defined for its identification description (id),
    the projection type (projection_type) and its internal calibration
    parameters, which depend on the particular Camera sub-class.

    Attributes:
        id (str): camera description.
        projection_type (str): projection type.
    """

    pass


class PerspectiveCamera(Camera):
    """Define a perspective camera.

    Attributes:
        width (int): image width.
        height (int): image height.
        focal (real): estimated focal length.
        k1 (real): estimated first distortion parameter.
        k2 (real): estimated second distortion parameter.
    """

    def __init__(self):
        """Defaut constructor."""
        self.id = None
        self.projection_type = 'perspective'
        self.width = None
        self.height = None
        self.focal = None
        self.k1 = None
        self.k2 = None

    def __repr__(self):
        return '{}({!r}, {!r}, {!r}, {!r}, {!r}, {!r}, {!r})'.format(
            self.__class__.__name__,
            self.id, self.projection_type, self.width, self.height,
            self.focal, self.k1, self.k2)

    def project(self, point):
        """Project a 3D point in camera coordinates to the image plane."""
        # Normalized image coordinates
        xn = point[0] / point[2]
        yn = point[1] / point[2]

        # Radial distortion
        r2 = xn * xn + yn * yn
        distortion = 1.0 + r2 * (self.k1 + self.k2 * r2)

        return np.array([self.focal * distortion * xn,
                         self.focal * distortion * yn])

    def project_many(self, points):
        """Project 3D points in camera coordinates to the image plane."""
        distortion = np.array([self.k1, self.k2, 0, 0, 0])
        K, R, t = self.get_K(), np.zeros(3), np.zeros(3)
        pixels, _ = cv2.projectPoints(points, R, t, K, distortion)
        return pixels.reshape((-1, 2))

    def pixel_bearing(self, pixel):
        """Unit vector pointing to the pixel viewing direction."""
        point = np.asarray(pixel).reshape((1, 1, 2))
        distortion = np.array([self.k1, self.k2, 0., 0.])
        x, y = cv2.undistortPoints(point, self.get_K(), distortion).flat
        l = np.sqrt(x * x + y * y + 1.0)
        return np.array([x / l, y / l, 1.0 / l])

    def pixel_bearing_many(self, pixels):
        """Unit vectors pointing to the pixel viewing directions."""
        points = pixels.reshape((-1, 1, 2)).astype(np.float64)
        distortion = np.array([self.k1, self.k2, 0., 0.])
        up = cv2.undistortPoints(points, self.get_K(), distortion)
        up = up.reshape((-1, 2))
        x = up[:, 0]
        y = up[:, 1]
        l = np.sqrt(x * x + y * y + 1.0)
        return np.column_stack((x / l, y / l, 1.0 / l))

    def pixel_bearings(self, pixels):
        """Deprecated: use pixel_bearing_many."""
        return self.pixel_bearing_many(pixels)

    def back_project(self, pixel, depth):
        """Project a pixel to a fronto-parallel plane at a given depth."""
        bearing = self.pixel_bearing(pixel)
        scale = depth / bearing[2]
        return scale * bearing

    def back_project_many(self, pixels, depths):
        """Project pixels to fronto-parallel planes at given depths."""
        bearings = self.pixel_bearing_many(pixels)
        scales = depths / bearings[:, 2]
        return scales[:, np.newaxis] * bearings

    def get_K(self):
        """The calibration matrix."""
        return np.array([[self.focal, 0., 0.],
                         [0., self.focal, 0.],
                         [0., 0., 1.]])

    def get_K_in_pixel_coordinates(self, width=None, height=None):
        """The calibration matrix that maps to pixel coordinates.

        Coordinates (0,0) correspond to the center of the top-left pixel,
        and (width - 1, height - 1) to the center of bottom-right pixel.

        You can optionally pass the width and height of the image, in case
        you are using a resized versior of the original image.
        """
        w = width or self.width
        h = height or self.height
        f = self.focal * max(w, h)
        return np.array([[f, 0, 0.5 * (w - 1)],
                         [0, f, 0.5 * (h - 1)],
                         [0, 0, 1.0]])


class BrownPerspectiveCamera(Camera):
    """Define a perspective camera.

    Attributes:
        width (int): image width.
        height (int): image height.
        focal_x (real): estimated focal length for the X axis.
        focal_y (real): estimated focal length for the Y axis.
        c_x (real): estimated principal point X.
        c_y (real): estimated principal point Y.
        k1 (real): estimated first radial distortion parameter.
        k2 (real): estimated second radial distortion parameter.
        p1 (real): estimated first tangential distortion parameter.
        p2 (real): estimated second tangential distortion parameter.
        k3 (real): estimated third radial distortion parameter.
    """

    def __init__(self):
        """Defaut constructor."""
        self.id = None
        self.projection_type = 'brown'
        self.width = None
        self.height = None
        self.focal_x = None
        self.focal_y = None
        self.c_x = None
        self.c_y = None
        self.k1 = None
        self.k2 = None
        self.p1 = None
        self.p2 = None
        self.k3 = None

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, self.__dict__)

    def project(self, point):
        """Project a 3D point in camera coordinates to the image plane."""
        # Normalized image coordinates
        xn = point[0] / point[2]
        yn = point[1] / point[2]

        # Radial and tangential distortion
        r2 = xn * xn + yn * yn
        radial_distortion = 1.0 + r2 * (self.k1 + r2 * (self.k2 + r2 * self.k3))
        x_tangential_distortion = 2 * self.p1 * xn * yn + self.p2 * (r2 + 2 * xn * xn)
        x_distorted = xn * radial_distortion + x_tangential_distortion
        y_tangential_distortion = self.p1 * (r2 + 2 * yn * yn) + 2 * self.p2 * xn * yn
        y_distorted = yn * radial_distortion + y_tangential_distortion

        return np.array([self.focal_x * x_distorted + self.c_x,
                         self.focal_y * y_distorted + self.c_y])

    def project_many(self, points):
        """Project 3D points in camera coordinates to the image plane."""
        distortion = np.array([self.k1, self.k2, self.p1, self.p2, self.k3])
        K, R, t = self.get_K(), np.zeros(3), np.zeros(3)
        pixels, _ = cv2.projectPoints(points, R, t, K, distortion)
        return pixels.reshape((-1, 2))

    def pixel_bearing(self, pixel):
        """Unit vector pointing to the pixel viewing direction."""
        point = np.asarray(pixel).reshape((1, 1, 2))
        distortion = np.array([self.k1, self.k2, self.p1, self.p2, self.k3])
        x, y = cv2.undistortPoints(point, self.get_K(), distortion).flat
        l = np.sqrt(x * x + y * y + 1.0)
        return np.array([x / l, y / l, 1.0 / l])

    def pixel_bearing_many(self, pixels):
        """Unit vector pointing to the pixel viewing directions."""
        points = pixels.reshape((-1, 1, 2)).astype(np.float64)
        distortion = np.array([self.k1, self.k2, self.p1, self.p2, self.k3])
        up = cv2.undistortPoints(points, self.get_K(), distortion)
        up = up.reshape((-1, 2))
        x = up[:, 0]
        y = up[:, 1]
        l = np.sqrt(x * x + y * y + 1.0)
        return np.column_stack((x / l, y / l, 1.0 / l))

    def pixel_bearings(self, pixels):
        """Deprecated: use pixel_bearing_many."""
        return self.pixel_bearing_many(pixels)

    def back_project(self, pixel, depth):
        """Project a pixel to a fronto-parallel plane at a given depth."""
        bearing = self.pixel_bearing(pixel)
        scale = depth / bearing[2]
        return scale * bearing

    def back_project_many(self, pixels, depths):
        """Project pixels to fronto-parallel planes at given depths."""
        bearings = self.pixel_bearing_many(pixels)
        scales = depths / bearings[:, 2]
        return scales[:, np.newaxis] * bearings

    def get_K(self):
        """The calibration matrix."""
        return np.array([[self.focal_x, 0., self.c_x],
                         [0., self.focal_y, self.c_y],
                         [0., 0., 1.]])

    def get_K_in_pixel_coordinates(self, width=None, height=None):
        """The calibration matrix that maps to pixel coordinates.

        Coordinates (0,0) correspond to the center of the top-left pixel,
        and (width - 1, height - 1) to the center of bottom-right pixel.

        You can optionally pass the width and height of the image, in case
        you are using a resized versior of the original image.
        """
        w = width or self.width
        h = height or self.height
        s = max(w, h)
        normalized_to_pixel = np.array([
            [s, 0, (w - 1) / 2.0],
            [0, s, (h - 1) / 2.0],
            [0, 0, 1],
        ])
        return np.dot(normalized_to_pixel, self.get_K())


class FisheyeCamera(Camera):
    """Define a fisheye camera.

    Attributes:
        width (int): image width.
        height (int): image height.
        focal (real): estimated focal length.
        k1 (real): estimated first distortion parameter.
        k2 (real): estimated second distortion parameter.
    """

    def __init__(self):
        """Defaut constructor."""
        self.id = None
        self.projection_type = 'fisheye'
        self.width = None
        self.height = None
        self.focal = None
        self.k1 = None
        self.k2 = None

    def project(self, point):
        """Project a 3D point in camera coordinates to the image plane."""
        x, y, z = point
        l = np.sqrt(x**2 + y**2)
        theta = np.arctan2(l, z)
        theta_d = theta * (1.0 + theta**2 * (self.k1 + theta**2 * self.k2))
        s = self.focal * theta_d / l
        return np.array([s * x, s * y])

    def project_many(self, points):
        """Project 3D points in camera coordinates to the image plane."""
        points = points.reshape((-1, 1, 3)).astype(np.float64)
        distortion = np.array([self.k1, self.k2, 0., 0.])
        K, R, t = self.get_K(), np.zeros(3), np.zeros(3)
        pixels, _ = cv2.fisheye.projectPoints(points, R, t, K, distortion)
        return pixels.reshape((-1, 2))

    def pixel_bearing(self, pixel):
        """Unit vector pointing to the pixel viewing direction."""
        point = np.asarray(pixel).reshape((1, 1, 2))
        distortion = np.array([self.k1, self.k2, 0., 0.])
        x, y = cv2.fisheye.undistortPoints(point, self.get_K(), distortion).flat
        l = np.sqrt(x * x + y * y + 1.0)
        return np.array([x / l, y / l, 1.0 / l])

    def pixel_bearing_many(self, pixels):
        """Unit vector pointing to the pixel viewing directions."""
        points = pixels.reshape((-1, 1, 2)).astype(np.float64)
        distortion = np.array([self.k1, self.k2, 0., 0.])
        up = cv2.fisheye.undistortPoints(points, self.get_K(), distortion)
        up = up.reshape((-1, 2))
        x = up[:, 0]
        y = up[:, 1]
        l = np.sqrt(x * x + y * y + 1.0)
        return np.column_stack((x / l, y / l, 1.0 / l))

    def pixel_bearings(self, pixels):
        """Deprecated: use pixel_bearing_many."""
        return self.pixel_bearing_many(pixels)

    def back_project(self, pixel, depth):
        """Project a pixel to a fronto-parallel plane at a given depth."""
        bearing = self.pixel_bearing(pixel)
        scale = depth / bearing[2]
        return scale * bearing

    def back_project_many(self, pixels, depths):
        """Project pixels to fronto-parallel planes at given depths."""
        bearings = self.pixel_bearing_many(pixels)
        scales = depths / bearings[:, 2]
        return scales[:, np.newaxis] * bearings

    def get_K(self):
        """The calibration matrix."""
        return np.array([[self.focal, 0., 0.],
                         [0., self.focal, 0.],
                         [0., 0., 1.]])

    def get_K_in_pixel_coordinates(self, width=None, height=None):
        """The calibration matrix that maps to pixel coordinates.

        Coordinates (0,0) correspond to the center of the top-left pixel,
        and (width - 1, height - 1) to the center of bottom-right pixel.

        You can optionally pass the width and height of the image, in case
        you are using a resized version of the original image.
        """
        w = width or self.width
        h = height or self.height
        f = self.focal * max(w, h)
        return np.array([[f, 0, 0.5 * (w - 1)],
                         [0, f, 0.5 * (h - 1)],
                         [0, 0, 1.0]])


class DualCamera(Camera):
    """Define a camera that seamlessly transition
        between fisheye and perspective camera.

    Attributes:
        width (int): image width.
        height (int): image height.
        focal (real): estimated focal length.
        k1 (real): estimated first distortion parameter.
        k2 (real): estimated second distortion parameter.
        transition (real): parametrize between perpective (1.0) and fisheye (0.0)
    """
    def __init__(self, projection_type='unknown'):
        """Defaut constructor."""
        self.id = None
        self.projection_type = 'dual'
        self.width = None
        self.height = None
        self.focal = None
        self.k1 = None
        self.k2 = None
        if projection_type == 'perspective':
            self.transition = 1.0
        elif projection_type == 'fisheye':
            self.transition = 0.0
        else:
            self.transition = 0.5

    def project(self, point):
        """Project a 3D point in camera coordinates to the image plane."""
        x, y, z = point
        l = np.sqrt(x**2 + y**2)
        theta = np.arctan2(l, z)
        x_fish = theta / l * x
        y_fish = theta / l * y

        x_persp = point[0] / point[2]
        y_persp = point[1] / point[2]

        x_dual = self.transition*x_persp + (1.0 - self.transition)*x_fish
        y_dual = self.transition*y_persp + (1.0 - self.transition)*y_fish

        r2 = x_dual * x_dual + y_dual * y_dual
        distortion = 1.0 + r2 * (self.k1 + self.k2 * r2)

        return np.array([self.focal * distortion * x_dual,
                         self.focal * distortion * y_dual])

    def project_many(self, points):
        """Project 3D points in camera coordinates to the image plane."""
        projected = []
        for point in points:
            projected.append(self.project(point))
        return np.array(projected)

    def pixel_bearing(self, pixel):
        """Unit vector pointing to the pixel viewing direction."""

        point = np.asarray(pixel).reshape((1, 1, 2))
        distortion = np.array([self.k1, self.k2, 0., 0.])
        no_K = np.array([[1., 0., 0.],
                         [0., 1., 0.],
                         [0., 0., 1.]])

        point = point / self.focal
        x_u, y_u = cv2.undistortPoints(point, no_K, distortion).flat
        r = np.sqrt(x_u**2 + y_u**2)

        # inverse iteration for finding theta from r
        theta = 0
        for i in range(5):
            f = self.transition*math.tan(theta) + (1.0 - self.transition)*theta - r
            secant = 1.0/math.cos(theta)
            d = (self.transition*secant**2 - self.transition + 1)
            if i < 1:
                theta -= 0.5*f/d
            else:
                theta -= f/d

        s = math.tan(theta)/(self.transition*math.tan(theta) + (1.0 - self.transition)*theta)
        x_dual = x_u*s
        y_dual = y_u*s

        l = math.sqrt(x_dual * x_dual + y_dual * y_dual + 1.0)
        return np.array([x_dual / l, y_dual / l, 1.0 / l])

    def pixel_bearing_many(self, pixels):
        """Unit vector pointing to the pixel viewing directions."""
        points = pixels.reshape((-1, 1, 2)).astype(np.float64)
        distortion = np.array([self.k1, self.k2, 0., 0.])
        no_K = np.array([[1., 0., 0.],
                         [0., 1., 0.],
                         [0., 0., 1.]])

        points = points / self.focal
        undistorted = cv2.undistortPoints(points, no_K, distortion)
        undistorted = undistorted.reshape((-1, 2))
        r = np.sqrt(undistorted[:, 0]**2 + undistorted[:, 1]**2)

        # inverse iteration for finding theta from r
        theta = 0
        for i in range(5):
            f = self.transition*np.tan(theta) + (1.0 - self.transition)*theta - r
            secant = 1.0/np.cos(theta)
            d = (self.transition*secant**2 - self.transition + 1)
            if i < 1:
                theta -= 0.5*f/d
            else:
                theta -= f/d

        s = np.tan(theta)/(self.transition*np.tan(theta) + (1.0 - self.transition)*theta)
        x_dual = undistorted[:, 0]*s
        y_dual = undistorted[:, 1]*s

        l = np.sqrt(x_dual * x_dual + y_dual * y_dual + 1.0)
        return np.column_stack([x_dual / l, y_dual / l, 1.0 / l])

    def pixel_bearings(self, pixels):
        """Deprecated: use pixel_bearing_many."""
        return self.pixel_bearing_many(pixels)

    def back_project(self, pixel, depth):
        """Project a pixel to a fronto-parallel plane at a given depth."""
        bearing = self.pixel_bearing(pixel)
        scale = depth / bearing[2]
        return scale * bearing

    def back_project_many(self, pixels, depths):
        """Project pixels to fronto-parallel planes at given depths."""
        bearings = self.pixel_bearing_many(pixels)
        scales = depths / bearings[:, 2]
        return scales[:, np.newaxis] * bearings

    def get_K(self):
        """The calibration matrix."""
        return np.array([[self.focal, 0., 0.],
                         [0., self.focal, 0.],
                         [0., 0., 1.]])

    def get_K_in_pixel_coordinates(self, width=None, height=None):
        """The calibration matrix that maps to pixel coordinates.

        Coordinates (0,0) correspond to the center of the top-left pixel,
        and (width - 1, height - 1) to the center of bottom-right pixel.

        You can optionally pass the width and height of the image, in case
        you are using a resized version of the original image.
        """
        w = width or self.width
        h = height or self.height
        f = self.focal * max(w, h)
        return np.array([[f, 0, 0.5 * (w - 1)],
                         [0, f, 0.5 * (h - 1)],
                         [0, 0, 1.0]])


class SphericalCamera(Camera):
    """A spherical camera generating equirectangular projections.

    Attributes:
        width (int): image width.
        height (int): image height.
    """

    def __init__(self):
        """Defaut constructor."""
        self.id = None
        self.projection_type = 'equirectangular'
        self.width = None
        self.height = None

    def project(self, point):
        """Project a 3D point in camera coordinates to the image plane."""
        x, y, z = point
        lon = np.arctan2(x, z)
        lat = np.arctan2(-y, np.sqrt(x**2 + z**2))
        return np.array([lon / (2 * np.pi), -lat / (2 * np.pi)])

    def project_many(self, points):
        """Project 3D points in camera coordinates to the image plane."""
        x, y, z = points.T
        lon = np.arctan2(x, z)
        lat = np.arctan2(-y, np.sqrt(x**2 + z**2))
        return np.column_stack([lon / (2 * np.pi), -lat / (2 * np.pi)])

    def pixel_bearing(self, pixel):
        """Unit vector pointing to the pixel viewing direction."""
        lon = pixel[0] * 2 * np.pi
        lat = -pixel[1] * 2 * np.pi
        x = np.cos(lat) * np.sin(lon)
        y = -np.sin(lat)
        z = np.cos(lat) * np.cos(lon)
        return np.array([x, y, z])

    def pixel_bearing_many(self, pixels):
        """Unit vector pointing to the pixel viewing directions."""
        lon = pixels[:, 0] * 2 * np.pi
        lat = -pixels[:, 1] * 2 * np.pi
        x = np.cos(lat) * np.sin(lon)
        y = -np.sin(lat)
        z = np.cos(lat) * np.cos(lon)
        return np.column_stack([x, y, z]).astype(float)

    def pixel_bearings(self, pixels):
        """Deprecated: use pixel_bearing_many."""
        return self.pixel_bearing_many(pixels)


def test_reconstruction_class_initialization():

    # Instantiate Reconstruction
    reconstruction = types.Reconstruction()
    focal = 0.9722222222222222
    k1 = 0.006094395128698237
    k2 = -0.0004952058188617129
    # Instantiate camera instrinsics
    camera = pygeometry.Camera.create_perspective(focal, k1, k2)
    camera.id = 'apple iphone 4s back camera 4.28mm f/2.4'
    camera.height = 2448
    camera.width = 3264
    reconstruction.add_camera(camera)

    # Instantiate GPS data
    metadata = pymap.ShotMeasurements()
    metadata.orientation.value = 1
    metadata.capture_time.value = 0.0
    metadata.gps_accuracy.value = 5.0
    metadata.gps_position.value = [1.0815875281451939,
                                   -0.96510451436708888,
                                   1.2042133903991235]
    metadata.accelerometer.value = [0.1, 0.9, 0.0]
    metadata.compass_angle.value = 270.0
    metadata.compass_accuracy.value = 15.0 
    metadata.sequence_key.value = 'a_sequence_key'

    # Instantiate shots
    pose0 = pygeometry.Pose([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    shot0 = reconstruction.create_shot('0', camera.id, pose0)
    shot0.metadata = metadata

    pose1 = pygeometry.Pose([0.0, 0.0, 0.0], [-1.0, 0.0, 0.0])
    shot1 = reconstruction.create_shot('1', camera.id, pose1)
    shot1.metadata = metadata

    # TEST
    assert len(reconstruction.cameras) == 1
    assert len(reconstruction.shots) == 2
    assert len(reconstruction.points) == 0
    assert reconstruction.get_camera(camera.id) is not None

    assert reconstruction.get_camera('1') is None
    assert reconstruction.get_shot(shot0.id) is not None
    assert reconstruction.get_shot(shot1.id) is not None
    assert reconstruction.get_shot('2') is None


def test_perspective_camera_projection():
    """Test perspectiive projection--backprojection loop."""
    for camera in _get_perspective_camera():
        pixel = [0.1, 0.2]
        bearing = camera.pixel_bearing(pixel)
        projected = camera.project(bearing)
        assert np.allclose(pixel, projected)


def test_fisheye_camera_projection():
    """Test fisheye projection--backprojection loop."""
    if not context.OPENCV3:
        return
    for camera in _get_fisheye_camera():
        pixel = [0.1, 0.2]
        bearing = camera.pixel_bearing(pixel)
        projected = camera.project(bearing)
        assert np.allclose(pixel, projected)


def test_dual_camera_projection():
    """Test dual projection--backprojection loop."""
    if not context.OPENCV3:
        return
    for camera in _get_dual_camera():
        pixel = [0.1, 0.2]
        bearing = camera.pixel_bearing(pixel)
        projected = camera.project(bearing)
        assert np.allclose(pixel, projected)


def test_spherical_camera_projection():
    """Test spherical projection--backprojection loop."""
    for camera in _get_spherical_camera():
        pixel = [0.1, 0.2]
        bearing = camera.pixel_bearing(pixel)
        projected = camera.project(bearing)
        assert np.allclose(pixel, projected)


# def test_pose_properties():
#     """Test pose constructor, getters and setters."""
#     p = pygeometry.Pose([1, 2, 3], [4, 5, 6])
#     assert np.allclose(p.rotation, [1, 2, 3])
#     assert type(p.rotation) == np.ndarray
#     assert p.rotation.dtype == float
#     assert np.allclose(p.translation, [4, 5, 6])
#     assert type(p.translation) == np.ndarray
#     assert p.translation.dtype == float


# def test_pose_inverse():
#     p = pygeometry.Pose([1, 2, 3], [4, 5, 6])
#     inverse = p.inverse()
#     identity = p.compose(inverse)
#     assert np.allclose(identity.rotation, [0, 0, 0])
#     assert np.allclose(identity.translation, [0, 0, 0])


def test_shot_project_back_project():
    pixels = np.array([[0.1, 0.2], [-0.1, 0.2]], dtype=float)
    depths = np.array([1, 2], dtype=float)
    pose = types.Pose([1, 2, 3], [4, 5, 6])
    cameras = [
        _get_perspective_camera(),
        _get_brown_perspective_camera(),
        _get_spherical_camera(),
    ]
    if context.OPENCV3:
        cameras.append(_get_fisheye_camera())

    shot = types.Shot()
    shot.pose = pose
    for pair in cameras:
        for cam in pair:
            shot.camera = cam
            bp_single = [shot.back_project(p,d) for p,d in zip(pixels, depths)]
            bp_many = shot.back_project_many(pixels, depths)
            assert np.allclose(bp_single, bp_many), cam.projection_type

            px_single = [shot.project(p) for p in bp_single]
            px_many = shot.project_many(bp_many)

            assert np.allclose(pixels, px_single), cam.projection_type
            assert np.allclose(pixels, px_many), cam.projection_type


def test_single_vs_many():
    points = np.array([[1, 2, 3], [4, 5, 6]], dtype=float)
    pixels = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=float)
    depths = np.array([1, 2], dtype=float)

    pose = pygeometry.Pose([1, 2, 3], [4, 5, 6])
    t_single = [pose.transform(p) for p in points]
    t_many = pose.transform_many(points)
    print("t_single", t_single)
    print("t_many", t_many)
    assert np.allclose(t_single, t_many)

    t_single = [pose.transform_inverse(p) for p in points]
    t_many = pose.transform_inverse_many(points)
    assert np.allclose(t_single, t_many)

    cameras = [
        _get_perspective_camera(),
        _get_brown_perspective_camera(),
        _get_spherical_camera(),
    ]
    if context.OPENCV3:
        cameras.append(_get_fisheye_camera())

    for camera, camera_cpp in cameras:
        p = camera.project_many(points)
        p_cpp = camera_cpp.project_many(points)
        assert np.allclose(p, p_cpp)

        b = camera.pixel_bearing_many(pixels)
        b_cpp = camera_cpp.pixel_bearing_many(pixels)
        assert np.allclose(b, b_cpp)

        if hasattr(camera, 'back_project'):
            q_single = [camera.back_project(p, d)
                        for p, d in zip(pixels, depths)]
            q_many = camera.back_project_many(pixels, depths)
            assert np.allclose(q_single, q_many)


def _get_perspective_camera():
    camera = PerspectiveCamera()
    camera.width = 800
    camera.height = 600
    camera.focal = 0.6
    camera.k1 = -0.1
    camera.k2 = 0.01
    camera_cpp = pygeometry.Camera.create_perspective(
        camera.focal, camera.k1, camera.k2)
    return camera, camera_cpp


def _get_brown_perspective_camera():
    camera = BrownPerspectiveCamera()
    camera.width = 800
    camera.height = 600
    camera.focal_x = 0.6
    camera.focal_y = 0.7
    camera.c_x = 0.1
    camera.c_y = -0.05
    camera.k1 = -0.1
    camera.k2 = 0.01
    camera.p1 = 0.001
    camera.p2 = 0.002
    camera.k3 = 0.01
    camera_cpp = pygeometry.Camera.create_brown(
        camera.focal_x, camera.focal_y / camera.focal_x,
        [camera.c_x, camera.c_y],
        [camera.k1, camera.k2, camera.k3, camera.p1, camera.p2])
    return camera, camera_cpp


def _get_fisheye_camera():
    camera = FisheyeCamera()
    camera.width = 800
    camera.height = 600
    camera.focal = 0.6
    camera.k1 = -0.1
    camera.k2 = 0.01
    camera_cpp = pygeometry.Camera.create_fisheye(
        camera.focal, camera.k1, camera.k2)
    return camera, camera_cpp


def _get_dual_camera():
    camera = DualCamera()
    camera.width = 800
    camera.height = 600
    camera.focal = 0.3
    camera.k1 = -0.1
    camera.k2 = 0.01
    camera.transition = 0.5
    camera_cpp = pygeometry.Camera.create_dual(
        camera.transition, camera.focal, camera.k1, camera.k2)
    return camera, camera_cpp


def _get_spherical_camera():
    camera = SphericalCamera()
    camera.width = 800
    camera.height = 600
    camera_cpp = pygeometry.Camera.create_spherical()
    return camera, camera_cpp


def test_shot_measurement():
    m = pymap.ShotMeasurementInt()
    assert not m.has_value
    m.value = 4
    assert m.has_value
    assert m.value == 4


def _helper_compare_poses(py_pose, cpp_pose):
    assert np.allclose(py_pose.translation, cpp_pose.translation)
    assert np.allclose(py_pose.rotation, cpp_pose.rotation)
    assert np.allclose(py_pose.get_rotation_matrix(), cpp_pose.get_rotation_matrix())
    assert np.allclose(py_pose.get_origin(), cpp_pose.get_origin())


def test_python_vs_cpp_pose():
    # identity pose
    py_pose = Pose()
    cpp_pose = pygeometry.Pose()
    _helper_compare_poses(py_pose, cpp_pose)

    R_cw = special_ortho_group.rvs(3)
    t_cw = np.random.rand(3)
    py_pose = Pose(cv2.Rodrigues(R_cw)[0].flatten(), t_cw)
    cpp_pose = pygeometry.Pose(R_cw, t_cw)
    _helper_compare_poses(py_pose, cpp_pose)

    new_origin =  np.random.rand(3)
    py_pose.set_origin(new_origin)
    cpp_pose.set_origin(new_origin)
    _helper_compare_poses(py_pose, cpp_pose)

    R_cw_2 = special_ortho_group.rvs(3)
    t_cw_2 = np.random.rand(3)
    py_pose_2 = Pose(cv2.Rodrigues(R_cw_2)[0].flatten(), t_cw_2)
    cpp_pose_2 = pygeometry.Pose(R_cw_2, t_cw_2)
    _helper_compare_poses(py_pose_2, cpp_pose_2)
    # _helper_compare_poses(py_pose_2.compose(py_pose), cpp_pose.relative_to(cpp_pose_2))
    _helper_compare_poses(py_pose.compose(py_pose_2.inverse()), cpp_pose.relative_to(cpp_pose_2))
    # # print("cpp_pose.relative_to(cpp_pose_2): ", cpp_pose_2.relative_to(cpp_pose))
    # # print("py_pose.relative_to(py_pose_2): ", py_pose.compose(py_pose_2))
    # # assert np.allclose(cpp_pose.relative_to(cpp_pose_2), py_pose_2.compose(py_pose))
    # # assert np.allclose(cpp_pose.relative_to(cpp_pose_2), py_pose.compose(py_pose_2))

    # # py_pose = Pose()
    # # cpp_pose = pygeometry.Pose()
    # # _helper_compare_poses(py_pose, cpp_pose)


def test_pose():
    pose = pygeometry.Pose()
    # Test default
    assert np.allclose(pose.get_cam_to_world(), np.eye(4), 1e-10)
    assert np.allclose(pose.get_world_to_cam(), np.eye(4), 1e-10)

    # Test setters and getters for translation
    R_cw = special_ortho_group.rvs(3)
    t_cw = np.random.rand(3)
    T_cw = np.vstack((np.column_stack((R_cw, t_cw)), np.array([0, 0, 0, 1])))
    pose = pygeometry.Pose()
    pose.set_from_world_to_cam(T_cw)
    assert np.allclose(pose.get_world_to_cam(), T_cw)
    assert np.allclose(pose.get_cam_to_world(), np.linalg.inv(T_cw))
    assert np.allclose(pose.get_origin(), pose.get_t_cam_to_world())

    pose2 = pygeometry.Pose()
    pose2.set_from_world_to_cam(R_cw, t_cw)
    assert np.allclose(pose2.get_world_to_cam(), T_cw)
    assert np.allclose(pose2.get_cam_to_world(), np.linalg.inv(T_cw))
    assert np.allclose(pose2.get_origin(), pose.get_t_cam_to_world())

    # test the rotation matrix
    assert np.allclose(pose.get_rotation_matrix(), R_cw)
    assert np.allclose(pose.get_R_world_to_cam(), R_cw)
    assert np.allclose(pose.get_R_cam_to_world(), R_cw.transpose())

    pose = pygeometry.Pose()
    R_wc = special_ortho_group.rvs(3)
    t_wc = np.random.rand(3)
    T_wc = np.vstack((np.column_stack((R_wc, t_wc)), np.array([0, 0, 0, 1])))
    pose.set_from_cam_to_world(T_wc)
    assert np.allclose(pose.get_cam_to_world(), T_wc)
    assert np.allclose(pose.get_world_to_cam(), np.linalg.inv(T_wc))
    assert np.allclose(pose.get_R_cam_to_world(), R_wc)
    assert np.allclose(pose.get_R_world_to_cam(), R_wc.transpose())
    assert np.allclose(pose.get_t_cam_to_world(), t_wc)
    assert np.allclose(pose.get_t_world_to_cam(), -R_wc.transpose().dot(t_wc))

    pose2 = pygeometry.Pose()
    pose2.set_from_cam_to_world(R_wc, t_wc)
    assert np.allclose(pose2.get_cam_to_world(), T_wc)
    assert np.allclose(pose2.get_world_to_cam(), np.linalg.inv(T_wc))
    assert np.allclose(pose2.get_origin(), pose.get_t_cam_to_world())

    pose.translation = t_cw
    assert np.allclose(pose.translation, t_cw)
    assert np.allclose(pose.get_t_world_to_cam(), t_cw)


def test_pose_minimal_representation():

    p1 = pygeometry.Pose()
    # Check identity pose
    p1.set_from_world_to_cam(np.array([0, 0, 0]), np.array([0, 0, 0]))
    assert np.allclose(p1.get_rotation_matrix(), np.eye(3))
    assert np.allclose(p1.get_R_world_to_cam_min(), np.zeros((1, 3)))
    assert np.allclose(p1.get_R_cam_to_world_min(), np.zeros((1, 3)))
    assert np.allclose(p1.get_cam_to_world(), np.eye(4))
    assert np.allclose(p1.get_world_to_cam(), np.eye(4))

    R_wc = special_ortho_group.rvs(3)
    t_wc = np.random.rand(3)
    pose = pygeometry.Pose()
    r_wc = Rotation.from_dcm(R_wc).as_rotvec()  # same as cv2.Rodrigues
    pose.set_from_cam_to_world(R_wc, t_wc)
    assert np.allclose(pose.get_R_cam_to_world_min(), r_wc)
    pose.set_from_world_to_cam(R_wc, t_wc)
    assert np.allclose(pose.get_R_world_to_cam_min(), r_wc)

    R_wc = special_ortho_group.rvs(3)
    t_wc = np.random.rand(3)
    pose = pygeometry.Pose()
    r_wc = Rotation.from_dcm(R_wc).as_rotvec()  # same as cv2.Rodrigues
    pose.set_from_world_to_cam(r_wc, t_wc)
    assert np.allclose(R_wc, pose.get_R_world_to_cam())
    assert np.allclose(pose.get_R_world_to_cam_min(), r_wc)
    assert np.allclose(pose.get_R_cam_to_world_min(), -r_wc)
    assert np.allclose(R_wc.transpose(), pose.get_R_cam_to_world())

    R_wc = special_ortho_group.rvs(3)
    pose = pygeometry.Pose()
    pose.set_rotation_matrix(R_wc)
    assert np.allclose(R_wc, pose.get_R_world_to_cam())
    r_wc = Rotation.from_dcm(R_wc).as_rotvec()
    assert np.allclose(r_wc, pose.get_R_world_to_cam_min())

    R_wc = special_ortho_group.rvs(3)
    pose = pygeometry.Pose()
    r_wc = Rotation.from_dcm(R_wc).as_rotvec()
    pose.rotation = r_wc

    assert np.allclose(R_wc, pose.get_R_world_to_cam())
    assert np.allclose(r_wc, pose.get_R_world_to_cam_min())
    r_cw = Rotation.from_dcm(R_wc.transpose()).as_rotvec()
    assert np.allclose(r_cw, pose.get_R_cam_to_world_min())

    # Test again all the setters and getters!
    R_wc = special_ortho_group.rvs(3)
    r_wc = Rotation.from_dcm(R_wc).as_rotvec()
    t_wc = np.random.rand(3)
    pose = pygeometry.Pose()
    pose.set_from_cam_to_world(r_wc, t_wc)
    assert np.allclose(pose.get_R_cam_to_world(), R_wc)
    assert np.allclose(pose.get_R_world_to_cam(), R_wc.transpose())
    assert np.allclose(pose.get_R_cam_to_world_min(), r_wc)
    assert np.allclose(pose.get_t_cam_to_world(), t_wc)

    pose = pygeometry.Pose()
    pose.set_from_cam_to_world(R_wc, t_wc)
    assert np.allclose(pose.get_R_cam_to_world(), R_wc)
    assert np.allclose(pose.get_R_world_to_cam(), R_wc.transpose())
    assert np.allclose(pose.get_R_cam_to_world_min(), r_wc)
    assert np.allclose(pose.get_t_cam_to_world(), t_wc)

    pose = pygeometry.Pose()
    T_wc = np.vstack((np.column_stack((R_wc, t_wc)), np.array([0, 0, 0, 1])))
    pose.set_from_cam_to_world(T_wc)
    assert np.allclose(pose.get_R_cam_to_world(), R_wc)
    assert np.allclose(pose.get_R_world_to_cam(), R_wc.transpose())
    assert np.allclose(pose.get_R_cam_to_world_min(), r_wc)
    assert np.allclose(pose.get_t_cam_to_world(), t_wc)
    assert np.allclose(pose.get_cam_to_world(), T_wc)

    # Test again
    R_cw = special_ortho_group.rvs(3)
    r_cw = Rotation.from_dcm(R_cw).as_rotvec()
    t_cw = np.random.rand(3)
    pose = pygeometry.Pose()
    pose.set_from_world_to_cam(r_cw, t_cw)
    assert np.allclose(pose.get_R_cam_to_world(), R_cw.transpose())
    assert np.allclose(pose.get_R_world_to_cam(), R_cw)
    assert np.allclose(pose.get_R_cam_to_world_min(), -r_cw)
    assert np.allclose(pose.get_R_world_to_cam_min(), r_cw)
    assert np.allclose(pose.get_t_world_to_cam(), t_cw)

    pose = pygeometry.Pose()
    pose.set_from_cam_to_world(R_cw, t_cw)
    assert np.allclose(pose.get_R_cam_to_world(), R_cw)
    assert np.allclose(pose.get_R_world_to_cam(), R_cw.transpose())
    assert np.allclose(pose.get_R_cam_to_world_min(), r_cw)
    assert np.allclose(pose.get_R_world_to_cam_min(), -r_cw)
    assert np.allclose(pose.get_t_cam_to_world(), t_cw)

    pose = pygeometry.Pose()
    T_cw = np.vstack((np.column_stack((R_cw, t_cw)), np.array([0, 0, 0, 1])))
    pose.set_from_cam_to_world(T_cw)
    assert np.allclose(pose.get_R_cam_to_world(), R_cw)
    assert np.allclose(pose.get_R_world_to_cam(), R_cw.transpose())
    assert np.allclose(pose.get_R_cam_to_world_min(), r_cw)
    assert np.allclose(pose.get_R_world_to_cam_min(), -r_cw)
    assert np.allclose(pose.get_t_cam_to_world(), t_cw)
    assert np.allclose(pose.get_cam_to_world(), T_cw)


test_python_vs_cpp_pose()
# R_cw = special_ortho_group.rvs(3)
# t_cw = np.random.rand(3)
# py_pose = Pose(cv2.Rodrigues(R_cw)[0].flatten(), t_cw)
# cpp_pose = pygeometry.Pose(R_cw, t_cw)
# # _helper_compare_poses(py_pose, cpp_pose)


# R_cw2 = special_ortho_group.rvs(3)
# t_cw2 = np.random.rand(3)
# py_pose2 = Pose(cv2.Rodrigues(R_cw2)[0].flatten(), t_cw2)
# cpp_pose2 = pygeometry.Pose(R_cw2, t_cw2)

# _helper_compare_poses(py_pose2, cpp_pose2)