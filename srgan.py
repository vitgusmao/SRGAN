from ESRGAN.data_manager import DataManager
from ESRGAN.losses import l1_loss
import glob
import os
import time
import ipdb
from keras.layers.core import Flatten

import numpy as np
import tensorflow as tf

from contextlib import contextmanager

from imageio import imread, imwrite
from keras import Input
from keras import backend as K
from keras_preprocessing.image import img_to_array, load_img
from keras.layers import BatchNormalization, Activation, LeakyReLU, Add, Dense, PReLU
from keras.layers.convolutional import Conv2D, UpSampling2D
from tensorflow.keras.applications import VGG19
from keras.models import Model, load_model
from keras.optimizers import Adam
from skimage.transform import resize as imresize


@contextmanager
def time_context(name):
    start_time = time.time()
    yield
    elapsed_time = time.time() - start_time
    print('[{}] terminou em {} ms'.format(name, int(elapsed_time * 1000)))


# Input shape
channels = 3
lr_height = 64  # Low resolution height
lr_width = 64  # Low resolution width
lr_shape = (lr_height, lr_width, channels)
hr_height = lr_height * 4  # High resolution height
hr_width = lr_width * 4  # High resolution width
hr_shape = (hr_height, hr_width, channels)

# Number of residual blocks in the generator
n_residual_blocks = 16

optimizer = Adam(lr=1E-4,
                 beta_1=0.9,
                 beta_2=0.999,
                 amsgrad=True,
                 epsilon=1e-08)

# Calculate output shape of D (PatchGAN)
patch = int(hr_height / 2**4)
disc_patch = (patch, patch, 1)

# Number of filters in the first layer of G and D
generator_filters = 64
discriminator_filters = 64

dataset_name = 'img_align_celeba'
dataset_dir = '../datasets/{}/'


data_loader = DataManager()


def preprocess_HR(x):
    return np.divide(x.astype(np.float32), 127.5) - np.ones_like(
        x, dtype=np.float32)


def deprocess_HR(x):
    x = (x + 1) * 127.5
    return x.astype(np.uint8)


def preprocess_LR(x):
    return np.divide(x.astype(np.float32), 255.)


def deprocess_LR(x):
    x = np.clip(x * 255, 0, 255)
    return x


vgg_model, vgg = build_vgg()
vgg.trainable = False
vgg.compile(loss='mse', optimizer=optimizer, metrics=['accuracy'])


def vgg_loss(y_true, y_pred):
    return K.mean(K.square(vgg_model(y_true) - vgg_model(y_pred)))


def build_generator():
    def residual_block(layer_input, filters):
        """Residual block described in paper"""
        block = Conv2D(filters=filters,
                       kernel_size=3,
                       strides=1,
                       padding="same")(layer_input)
        block = BatchNormalization(momentum=0.5)(block)
        block = PReLU(alpha_initializer='zeros',
                      alpha_regularizer=None,
                      alpha_constraint=None,
                      shared_axes=[1, 2])(block)
        block = Conv2D(filters, kernel_size=3, strides=1,
                       padding='same')(block)
        block = BatchNormalization(momentum=0.5)(block)
        block = Add()([layer_input, block])
        return block

    def deconv2d(layer_input):
        """Layers used during upsampling"""
        up_sampling = Conv2D(256, kernel_size=3, strides=1,
                             padding='same')(layer_input)
        up_sampling = UpSampling2D(size=2)(up_sampling)
        up_sampling = LeakyReLU(alpha=0.2)(up_sampling)
        return up_sampling

    # Low resolution image input
    img_lr = Input(shape=lr_shape)

    # Pre-residual block
    conv1 = Conv2D(64, kernel_size=9, strides=1, padding='same')(img_lr)
    conv1 = PReLU(alpha_initializer='zeros',
                  alpha_regularizer=None,
                  alpha_constraint=None,
                  shared_axes=[1, 2])(conv1)

    # Propogate through residual blocks
    residual_blocks = residual_block(conv1, generator_filters)
    for _ in range(n_residual_blocks - 1):
        residual_blocks = residual_block(residual_blocks, generator_filters)

    # Post-residual block
    conv2 = Conv2D(64, kernel_size=3, strides=1,
                   padding='same')(residual_blocks)
    conv2 = BatchNormalization(momentum=0.5)(conv2)
    conv2 = Add()([conv1, conv2])

    # Upsampling
    up_sampling1 = deconv2d(conv2)
    up_sampling2 = deconv2d(up_sampling1)

    # Generate high resolution output
    gen_hr = Conv2D(
        channels,
        kernel_size=9,
        strides=1,
        padding='same',
    )(up_sampling2)
    gen_hr = Activation('tanh')(gen_hr)

    return Model(img_lr, gen_hr)


def build_discriminator():
    def dis_block(layer_input, filters, strides=1, bn=True):
        """Discriminator layer"""
        dis = Conv2D(filters, kernel_size=3, strides=strides,
                     padding='same')(layer_input)
        if bn:
            dis = BatchNormalization(momentum=0.5)(dis)
        dis = LeakyReLU(alpha=0.2)(dis)
        return dis

    # Input img
    dis_input = Input(shape=hr_shape)

    dis = dis_block(dis_input, discriminator_filters, bn=False)
    dis = dis_block(dis, discriminator_filters, strides=2)
    dis = dis_block(dis, discriminator_filters * 2)
    dis = dis_block(dis, discriminator_filters * 2, strides=2)
    dis = dis_block(dis, discriminator_filters * 4)
    dis = dis_block(dis, discriminator_filters * 4, strides=2)
    dis = dis_block(dis, discriminator_filters * 8)
    dis = dis_block(dis, discriminator_filters * 8, strides=2)

    dis = Flatten()(dis)
    dis = Dense(discriminator_filters * 16)(dis)
    dis = LeakyReLU(alpha=0.2)(dis)

    validity = Dense(1)(dis)
    validity = Activation('sigmoid')(validity)

    return Model(dis_input, validity)


def sample_images(epoch=None, create_dirs=False):
    testing_batch_size = 2
    os.makedirs('imgs/%s' % dataset_name, exist_ok=True)
    if create_dirs:
        for i in range(testing_batch_size):
            os.makedirs('imgs/{}/{}'.format(dataset_name, i), exist_ok=True)

    hr_imgs, lr_imgs = data_loader.load_data(batch_size=testing_batch_size,
                                             is_testing=True)
    hr_fakes = generator.predict(lr_imgs)

    hr_fakes = denormalize(hr_fakes)

    if not create_dirs:
        for index, hr_gen in zip(range(len(hr_fakes)), hr_fakes):
            imwrite(
                'imgs/{}/{}/{}_{}.jpg'.format(dataset_name, index, epoch,
                                              'generated'),
                hr_gen.astype(np.uint8))

    else:
        for index, (hr_img, lr_img) in zip(range(len(hr_imgs)),
                                           zip(hr_imgs, lr_imgs)):
            imwrite(
                'imgs/{}/{}/{}.jpg'.format(dataset_name, index,
                                           '?0_high_resolution'),
                hr_img.astype(np.uint8))
            imwrite(
                'imgs/{}/{}/{}.jpg'.format(dataset_name, index,
                                           '?0_low_resolution'),
                lr_img.astype(np.uint8))


# Build the generator
generator = build_generator()
generator.compile(loss=vgg_loss, optimizer=optimizer)

discriminator = build_discriminator()
discriminator.compile(loss=['binary_crossentropy', l1_loss],
                      optimizer=optimizer,
                      metrics=['accuracy'])

# For the adversarial model we will only train the generator
discriminator.trainable = False

# low res. images
img_input = Input(shape=lr_shape)

# Generate high res. version from low res.
gen_hr = generator(img_input)

# Discriminator determines validity of generated high res. images
validity_output = discriminator(gen_hr)

adversarial = Model(inputs=img_input, outputs=[gen_hr, validity_output])
adversarial.compile(loss=[vgg_loss, 'binary_crossentropy'],
                    loss_weights=[1.0, 1e-3],
                    optimizer=optimizer)

epochs = 300
batch_size = 8
sample_interval = 10

sample_images(create_dirs=True)

with time_context('treino total'):
    with tf.device('/gpu:0') as GPU:
        for epoch in range(epochs):
            #  Train Discriminator
            # ----------------------

            imgs_hr, imgs_lr = data_loader.load_data(batch_size)
            imgs_hr = normalize(imgs_hr)
            imgs_lr = normalize(imgs_lr)

            # From low res. image generate high res. version
            fake_hr = generator.predict(imgs_lr)

            real_y = np.ones(
                batch_size) - np.random.random_sample(batch_size) * 0.2
            fake_y = np.random.random_sample(batch_size) * 0.1

            # ipdb.set_trace()

            discriminator.trainable = True

            # Train the discriminators (original images = real / generated = Fake)
            d_loss_real = discriminator.train_on_batch(imgs_hr, real_y)
            d_loss_fake = discriminator.train_on_batch(fake_hr, fake_y)
            d_loss = 0.5 * np.add(d_loss_real, d_loss_fake)

            # ------------------
            #  Train Generator
            # ------------------

            # Sample images and their conditioning counterparts
            imgs_hr, imgs_lr = data_loader.load_data(batch_size)
            imgs_hr = normalize(imgs_hr)
            imgs_lr = normalize(imgs_lr)

            discriminator.trainable = False

            # The generators want the discriminators to label the generated images as real
            real_y = np.ones(
                batch_size) - np.random.random_sample(batch_size) * 0.2

            # Extract ground truth image features using pre-trained VGG19 model
            vgg_y = vgg.predict(imgs_hr)

            # Train the generators
            g_loss = adversarial.train_on_batch(imgs_lr, [imgs_hr, vgg_y])

            # If at save interval => save generated image samples
            if epoch % sample_interval == 0:
                sample_images(epoch)