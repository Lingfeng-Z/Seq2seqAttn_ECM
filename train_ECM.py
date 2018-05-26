# -*- coding: utf-8 -*-
# @Author: aaronlai
# @Date:   2018-05-14 19:08:20
# @Last Modified by:   AaronLai
# @Last Modified time: 2018-05-25 18:39:30


from utils import init_embeddings, compute_ECM_loss, compute_perplexity, \
        loadfile, get_ECM_config, get_ECM_training_config, load, save

import argparse
import time
import yaml
import tensorflow as tf
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use('agg')

import matplotlib.pyplot as plt   # noqa


def parse_args():
    '''
    Parse Emotional Chatting Machine (ECM) arguments.
    '''
    parser = argparse.ArgumentParser(description="Run ECM training.")

    parser.add_argument('--config', nargs='?',
                        default='./configs/config_ECM.yaml',
                        help='Configuration file for model specifications')

    return parser.parse_args()


def main(args):
    # loading configurations
    with open(args.config) as f:
        config = yaml.safe_load(f)["configuration"]

    name = config["Name"]

    # Construct or load embeddings
    print("Initializing embeddings ...")
    vocab_size = config["embeddings"]["vocab_size"]
    embed_size = config["embeddings"]["embed_size"]
    embeddings = init_embeddings(vocab_size, embed_size, name=name)
    print("\tDone.")

    # Build the model and compute losses
    source_ids = tf.placeholder(tf.int32, [None, None], name="source")
    target_ids = tf.placeholder(tf.int32, [None, None], name="target")
    sequence_mask = tf.placeholder(tf.bool, [None, None], name="mask")
    choice_qs = tf.placeholder(tf.float32, [None, None], name="choice")
    emo_cat = tf.placeholder(tf.int32, [None], name="emotion_category")

    (enc_num_layers, enc_num_units, enc_cell_type, enc_bidir,
     dec_num_layers, dec_num_units, dec_cell_type, state_pass,
     num_emo, emo_cat_units, emo_int_units,
     infer_batch_size, beam_size, max_iter,
     attn_num_units, l2_regularize) = get_ECM_config(config)

    print("Building model architecture ...")
    CE, loss, train_outs, infer_outputs = compute_ECM_loss(
        source_ids, target_ids, sequence_mask, choice_qs, embeddings,
        enc_num_layers, enc_num_units, enc_cell_type, enc_bidir,
        dec_num_layers, dec_num_units, dec_cell_type, state_pass,
        num_emo, emo_cat, emo_cat_units, emo_int_units, infer_batch_size,
        beam_size, max_iter, attn_num_units, l2_regularize, name)
    print("\tDone.")

    # Even if we restored the model, we will treat it as new training
    # if the trained model is written into an arbitrary location.
    (logdir, restore_from, learning_rate, gpu_fraction, max_checkpoints,
     train_steps, batch_size, print_every, checkpoint_every, s_filename,
     t_filename, q_filename, c_filename, s_max_leng, t_max_leng,
     dev_s_filename, dev_t_filename, dev_q_filename, dev_c_filename,
     loss_fig, perp_fig) = get_ECM_training_config(config)

    is_overwritten_training = logdir != restore_from

    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate,
                                       epsilon=1e-4)
    trainable = tf.trainable_variables()
    optim = optimizer.minimize(loss, var_list=trainable)

    # Set up session
    gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=gpu_fraction)
    sess = tf.Session(config=tf.ConfigProto(log_device_placement=False,
                                            gpu_options=gpu_options))
    init = tf.global_variables_initializer()
    sess.run(init)

    # Saver for storing checkpoints of the model.
    saver = tf.train.Saver(var_list=tf.trainable_variables(),
                           max_to_keep=max_checkpoints)

    try:
        saved_global_step = load(saver, sess, restore_from)
        if is_overwritten_training or saved_global_step is None:
            # The first training step will be saved_global_step + 1,
            # therefore we put -1 here for new or overwritten trainings.
            saved_global_step = -1

    except Exception:
        print("Something went wrong while restoring checkpoint. "
              "Training is terminated to avoid the overwriting.")
        raise

    # ##### Training #####
    # Load data
    print("Loading data ...")

    # id_0, id_1, id_2 preserved for SOS, EOS, constant zero padding
    embed_shift = 3

    source_data = loadfile(s_filename, is_source=True,
                           max_length=s_max_leng) + embed_shift
    target_data = loadfile(t_filename, is_source=False,
                           max_length=t_max_leng) + embed_shift

    choice_data = loadfile(q_filename, is_source=False, max_length=t_max_leng)
    choice_data[choice_data < 0] = 0
    choice_data = choice_data.astype(np.float32)

    category_data = pd.read_csv(
        c_filename, header=None, index_col=None, dtype=int)[0].values

    masks = (target_data != -1)
    n_data = len(source_data)

    dev_source_data = None
    if dev_s_filename is not None:
        dev_source_data = loadfile(dev_s_filename, is_source=True,
                                   max_length=s_max_leng) + embed_shift
        dev_target_data = loadfile(dev_t_filename, is_source=False,
                                   max_length=t_max_leng) + embed_shift

        dev_choice_data = loadfile(dev_q_filename, is_source=False,
                                   max_length=t_max_leng)
        dev_choice_data[dev_choice_data < 0] = 0
        dev_choice_data = dev_choice_data.astype(np.float32)

        dev_category_data = pd.read_csv(
            dev_c_filename, header=None, index_col=None, dtype=int)[0].values

        dev_masks = (dev_target_data != -1)
    print("\tDone.")

    # Training
    last_saved_step = saved_global_step
    num_steps = saved_global_step + train_steps
    losses = []
    steps = []
    perps = []
    dev_perps = []

    print("Start training ...")
    try:
        for step in range(saved_global_step + 1, num_steps):
            start_time = time.time()
            rand_indexes = np.random.choice(n_data, batch_size)
            source_batch = source_data[rand_indexes]
            target_batch = target_data[rand_indexes]
            choice_batch = choice_data[rand_indexes]
            emotions = category_data[rand_indexes]
            mask_batch = masks[rand_indexes]

            feed_dict = {
                source_ids: source_batch,
                target_ids: target_batch,
                choice_qs: choice_batch,
                emo_cat: emotions,
                sequence_mask: mask_batch,
            }

            loss_value, _ = sess.run([loss, optim], feed_dict=feed_dict)
            losses.append(loss_value)

            duration = time.time() - start_time

            if step % print_every == 0:
                # train perplexity
                t_perp = compute_perplexity(sess, CE, mask_batch, feed_dict)
                perps.append(t_perp)

                # dev perplexity
                dev_str = ""
                if dev_source_data is not None:
                    dev_inds = np.random.choice(
                        len(dev_source_data), batch_size)

                    dev_feed_dict = {
                        source_ids: dev_source_data[dev_inds],
                        target_ids: dev_target_data[dev_inds],
                        choice_qs: dev_choice_data[dev_inds],
                        emo_cat: dev_category_data[dev_inds],
                        sequence_mask: dev_masks[dev_inds],
                    }

                    dev_perp = compute_perplexity(
                        sess, CE, dev_masks[dev_inds], dev_feed_dict)
                    dev_perps.append(dev_perp)
                    dev_str = "dev_prep: {:.3f}, ".format(dev_perp)

                steps.append(step)
                info = 'step {:d}, loss = {:.6f}, '
                info += 'perp: {:.3f}, {}({:.3f} sec/step)'
                print(info.format(step, loss_value, t_perp, dev_str, duration))

            if step % checkpoint_every == 0:
                save(saver, sess, logdir, step)
                last_saved_step = step

    except KeyboardInterrupt:
        # Introduce a line break after ^C so save message is on its own line.
        print()

    finally:
        if step > last_saved_step:
            save(saver, sess, logdir, step)

        # plot loss
        plt.figure()
        plt.plot(losses)
        plt.title("Total loss")
        plt.xlabel("step")
        plt.savefig(loss_fig)

        # plot perplexity
        plt.figure()
        if len(perps) > len(steps):
            perps.pop()
        plt.plot(steps[5:], perps[5:], label="train")
        if dev_source_data is not None:
            plt.plot(steps[5:], dev_perps[5:], label="dev")
        plt.title("Perplexity")
        plt.xlabel("step")
        plt.legend()
        plt.savefig(perp_fig)


if __name__ == "__main__":
    args = parse_args()
    main(args)
