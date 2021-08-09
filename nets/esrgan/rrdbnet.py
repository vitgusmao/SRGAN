from keras.engine.input_layer import Input
from keras.layers import LeakyReLU, Add, Concatenate
from keras.layers.convolutional import Conv2D, UpSampling2D
from keras.models import Model


import functools
import tensorflow as tf
from tensorflow.keras import Model

# from tensorflow.keras.layers import Dense, Flatten, Input, Conv2D, LeakyReLU


def _regularizer(weights_decay=5e-4):
    return tf.keras.regularizers.l2(weights_decay)


def _kernel_init(scale=1.0, seed=None):
    """He normal initializer with scale."""
    scale = 2.0 * scale
    return tf.keras.initializers.VarianceScaling(
        scale=scale, mode="fan_in", distribution="truncated_normal", seed=seed
    )


class ResDenseBlock_5C(tf.keras.layers.Layer):
    """Residual Dense Block"""

    def __init__(self, nf=64, gc=32, res_beta=0.2, wd=0.0, name="RDB5C", **kwargs):
        super(ResDenseBlock_5C, self).__init__(name=name, **kwargs)
        # gc: growth channel, i.e. intermediate channels
        self.res_beta = res_beta
        lrelu_f = functools.partial(LeakyReLU, alpha=0.2)
        _Conv2DLayer = functools.partial(
            Conv2D,
            kernel_size=3,
            padding="same",
            kernel_initializer=_kernel_init(0.1),
            bias_initializer="zeros",
            kernel_regularizer=_regularizer(wd),
        )
        self.conv1 = _Conv2DLayer(filters=gc, activation=lrelu_f())
        self.conv2 = _Conv2DLayer(filters=gc, activation=lrelu_f())
        self.conv3 = _Conv2DLayer(filters=gc, activation=lrelu_f())
        self.conv4 = _Conv2DLayer(filters=gc, activation=lrelu_f())
        self.conv5 = _Conv2DLayer(filters=nf, activation=lrelu_f())

    def call(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(tf.concat([x, x1], 3))
        x3 = self.conv3(tf.concat([x, x1, x2], 3))
        x4 = self.conv4(tf.concat([x, x1, x2, x3], 3))
        x5 = self.conv5(tf.concat([x, x1, x2, x3, x4], 3))
        return x5 * self.res_beta + x

    def get_config(self):
        config = super(ResDenseBlock_5C, self).get_config()
        config.update(
            {
                "res_beta": self.res_beta,
                "conv1": self.conv1,
                "conv2": self.conv2,
                "conv3": self.conv3,
                "conv4": self.conv4,
                "conv5": self.conv5,
            }
        )
        return config


class ResInResDenseBlock(tf.keras.layers.Layer):
    """Residual in Residual Dense Block"""

    def __init__(self, nf=64, gc=32, res_beta=0.2, wd=0.0, name="RRDB", **kwargs):
        super(ResInResDenseBlock, self).__init__(name=name, **kwargs)
        self.res_beta = res_beta
        self.rdb_1 = ResDenseBlock_5C(nf, gc, res_beta=res_beta, wd=wd)
        self.rdb_2 = ResDenseBlock_5C(nf, gc, res_beta=res_beta, wd=wd)
        self.rdb_3 = ResDenseBlock_5C(nf, gc, res_beta=res_beta, wd=wd)

    def call(self, x):
        out = self.rdb_1(x)
        out = self.rdb_2(out)
        out = self.rdb_3(out)
        return out * self.res_beta + x

    def get_config(self):
        config = super(ResDenseBlock_5C, self).get_config()
        config.update(
            {
                "res_beta": self.res_beta,
                "rdb_1": self.rdb_1,
                "rdb_2": self.rdb_2,
                "rdb_3": self.rdb_3,
            }
        )
        return config


# Based on https://github.com/peteryuX/esrgan-tf2 implementation of https://github.com/xinntao/BasicSR implementation
def RRDB_Model(
    gt_size, scale, channels, nf=64, nb=16, gc=32, wd=0.0, name="RRDB_model"
):

    size = int(gt_size / scale)

    lrelu_f = functools.partial(LeakyReLU, alpha=0.2)
    rrdb_f = functools.partial(ResInResDenseBlock, nf=nf, gc=gc, wd=wd)
    conv_f = functools.partial(
        Conv2D,
        kernel_size=3,
        padding="same",
        bias_initializer="zeros",
        kernel_initializer=_kernel_init(),
        kernel_regularizer=_regularizer(wd),
    )
    rrdb_truck_f = tf.keras.Sequential(
        [rrdb_f(name="RRDB_{}".format(i)) for i in range(nb)], name="RRDB_trunk"
    )

    # extraction
    x = inputs = Input([size, size, channels], name="input_image")
    fea = conv_f(filters=nf, name="conv_first")(x)
    fea_rrdb = rrdb_truck_f(fea)
    trunck = conv_f(filters=nf, name="conv_trunk")(fea_rrdb)
    fea = fea + trunck

    # upsampling
    size_fea_h = tf.shape(fea)[1] if size is None else size
    size_fea_w = tf.shape(fea)[2] if size is None else size
    fea_resize = tf.image.resize(
        fea, [size_fea_h * 2, size_fea_w * 2], method="nearest", name="upsample_nn_1"
    )
    fea = conv_f(filters=nf, activation=lrelu_f(), name="upconv_1")(fea_resize)
    fea_resize = tf.image.resize(
        fea, [size_fea_h * 4, size_fea_w * 4], method="nearest", name="upsample_nn_2"
    )
    fea = conv_f(filters=nf, activation=lrelu_f(), name="upconv_2")(fea_resize)
    fea = conv_f(filters=nf, activation=lrelu_f(), name="conv_hr")(fea)
    out = conv_f(filters=channels, name="conv_last")(fea)

    model = Model(inputs, out, name=name)

    # # Retrieve the config
    # config = model.get_config()

    # # At loading time, register the custom objects with a `custom_object_scope`:
    # custom_objects = {"ResInResDenseBlock": ResInResDenseBlock}
    # with tf.keras.utils.custom_object_scope(custom_objects):
    #     model = tf.keras.Model.from_config(config)

    model.summary(line_length=80)

    return model
