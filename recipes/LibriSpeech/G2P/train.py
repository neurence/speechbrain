#!/usr/bin/env/python3
"""Recipe for training a grapheme-to-phoneme system with one of the available datasets.

The following models are available:
* RNN-based (LSTM-encoder, GRU-decoder), with an attention mechanism
  * `hparams/hparams_attnrnn_lexicon.yaml`: LibriSpeech lexicon
  * `hparams/hparams_attnrnn_librig2p_nostress.yaml`: LibriG2P (no stress markers)
  * `hparams/hparams_attnrnn_librig2p_nostress_tok.yaml`: LibriG2P (no stress markers, tokenization)
* Convolutional
  * `hparams/hparams_conv_librig2p_nostress.yaml`
* Transformer
  * `hparams/hparams_transformer_librig2p_nostress.yaml`: LibriG2P (no stress markers)
  * `hparams/hparams_transformer_librig2p_nostress_tok.yaml`: LibriG2P (no stress markers, tokenization)

The datasets used here are available at the following locations:

* LibriG2P (no stress markers): https://github.com/flexthink/librig2p-nostress
* LibriG2P (no stress markers, spaces preserved): https://github.com/flexthink/librig2p-nostress-space

Decoding is performed with beamsearch.

To run this recipe, do the following:
> python train.py <hyperparameter file>
Example:
> python train.py hparams/hparams_attnrnn_librig2p_nostress.yaml

RNN Model
---------
With the default hyperparameters, the system employs an LSTM encoder.
The decoder is based on a standard  GRU. The neural network is trained with
negative-log.

Transformer Model
-----------------
With the default hyperparameters, the system employs a Conformer architecture
with a convolutional encoder and a standard Transformer decoder.

The choice of Conformer vs Transformer is controlled by the
transformer_encoder_module parameter.

Homograph Disambiguation
------------------------
Both RNN-based and Transformer-based models are capable of sentence-level
hyperparameter disambiguation. Fine-tuning on homographs relies on an additional
weighted loss computed only on the homograph and, therefore, requires a dataset
in which the homograph is labelled, such as LibriG2P.

The experiment file is flexible enough to support a large variety of
different systems. By properly changing the parameter files, you can try
different encoders, decoders,  and many other possible variations.

Hyperparameter Optimization
---------------------------
This recipe supports hyperparameter optimization via Oríon or other similar tools.
For details on how to set up hyperparameter optimization, refer to the
"Hyperparameter Optimization" tutorial in the Advanced Tutorials section
on the SpeechBrian website:

https://speechbrain.github.io/tutorial_advanced.html

A supplemental hyperparameter file is provided for hyperparameter optimiszation,
which will turn off checkpointing and limit the number of epochs:

hparams/hpfit.yaml


Authors
 * Loren Lugosch 2020
 * Mirco Ravanelli 2020
 * Artem Ploujnikov 2021
"""
from speechbrain.dataio.dataset import FilteredSortedDynamicItemDataset
from speechbrain.dataio.sampler import BalancingDataSampler
from speechbrain.utils.data_utils import undo_padding
import sys

import speechbrain as sb
import os
import random
from enum import Enum
from collections import namedtuple
from hyperpyyaml import load_hyperpyyaml
from speechbrain.utils.distributed import run_on_main
from speechbrain.pretrained.training import save_for_pretrained
from speechbrain.lobes.models.g2p.dataio import (
    enable_eos_bos,
    grapheme_pipeline,
    phoneme_pipeline,
    tokenizer_encode_pipeline,
    add_bos_eos,
    get_sequence_key,
)
from speechbrain.dataio.wer import print_alignments
from speechbrain.wordemb.util import expand_to_chars
from io import StringIO
from speechbrain.utils import hpopt as hp
import numpy as np


G2PPredictions = namedtuple(
    "G2PPredictions",
    "p_seq char_lens hyps ctc_logprobs attn",
    defaults=[None] * 4,
)


class TrainMode(Enum):
    """An enumeration that represents the trainining mode

    NORMAL: trains the sequence-to-sequence model
    HOMOGRAPH: fine-tunes a trained model on homographs"""

    NORMAL = "normal"
    HOMOGRAPH = "homograph"


# Define training procedure
class G2PBrain(sb.Brain):
    def __init__(self, train_step_name, *args, **kwargs):
        """Class constructor

        Arguments
        ---------
        train_step_name: the name of the training step, for curriculum learning
        """
        super().__init__(*args, **kwargs)
        self.train_step_name = train_step_name
        self.train_step = next(
            step
            for step in self.hparams.train_steps
            if step["name"] == train_step_name
        )
        self.epoch_counter = self.train_step["epoch_counter"]
        self.has_ctc = hasattr(self.hparams, "ctc_lin")
        self.mode = TrainMode(train_step.get("mode", TrainMode.NORMAL))
        self.last_attn = None
        self.use_word_emb = getattr(self.hparams, "use_word_emb", False)
        self.phn_tokenize = getattr(self.hparams, "phn_tokenize", False)
        self.lr_annealing_mode = getattr(
            self.hparams, "lr_annealing_mode", "epoch"
        )
        self.ckpt_frequency = getattr(self.hparams, "ckpt_frequency", 1)
        self.enable_metrics = getattr(self.hparams, "enable_metrics", True)
        self.lr_annealing = getattr(
            self.train_step, "lr_annealing", self.hparams.lr_annealing
        )
        self.phn_key = get_sequence_key(
            key="phn_encoded",
            mode=getattr(self.hparams, "phoneme_sequence_mode", "bos"),
        )
        self.grapheme_key = get_sequence_key(
            key="grapheme_encoded",
            mode=getattr(self.hparams, "grapheme_sequence_mode", "bos"),
        )
        self.beam_searcher = self.hparams.beam_searcher
        self.beam_searcher_valid = getattr(
            self.hparams, "beam_searcher_valid", self.hparams.beam_searcher
        )
        self.start_epoch = None

    def compute_forward(self, batch, stage):
        """Forward computations from the char batches to the output probabilities."""
        batch = batch.to(self.device)

        graphemes, grapheme_lens = getattr(batch, self.grapheme_key)
        phn_encoded = getattr(batch, self.phn_key)
        word_emb = None
        if self.use_word_emb:
            word_emb = self.modules.word_emb.batch_embeddings(batch.char)
            char_word_emb = expand_to_chars(
                emb=word_emb,
                seq=graphemes,
                seq_len=grapheme_lens,
                word_separator=self.grapheme_word_separator_idx,
            )
        else:
            word_emb, char_word_emb = None, None
        p_seq, char_lens, encoder_out, attn = self.modules["model"](
            grapheme_encoded=(graphemes.detach(), grapheme_lens),
            phn_encoded=phn_encoded,
            word_emb=char_word_emb,
        )

        self.last_attn = attn

        hyps = None
        ctc_logprobs = None
        if stage == sb.Stage.TRAIN and self.is_ctc_active(stage):
            # Output layer for ctc log-probabilities
            ctc_logits = self.modules.ctc_lin(encoder_out)
            ctc_logprobs = self.hparams.log_softmax(ctc_logits)

        if stage != sb.Stage.TRAIN and self.enable_metrics:
            beam_searcher = (
                self.beam_searcher_valid
                if stage == sb.Stage.VALID
                else self.beam_searcher
            )
            hyps, scores = beam_searcher(encoder_out, char_lens)

        return G2PPredictions(p_seq, char_lens, hyps, ctc_logprobs, attn)

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss (CTC+NLL) given predictions and targets.


        Arguments
        ---------
        predictions: G2PPredictions
            the predictions (as computed by compute_forward)
        batch: PaddedBatch
            a raw G2P data batch
        stage: speechbrain.Stage
            the training stage
        """
        ids = batch.id
        phns_eos, phn_lens_eos = batch.phn_encoded_eos
        phns, phn_lens = batch.phn_encoded
        loss_seq = self.hparams.seq_cost(
            predictions.p_seq, phns_eos, phn_lens_eos
        )
        if self.is_ctc_active(stage):
            seq_weight = 1 - self.hparams.ctc_weight
            loss_ctc = self.hparams.ctc_cost(
                predictions.ctc_logprobs,
                phns_eos,
                predictions.char_lens,
                phn_lens_eos,
            )
            loss = seq_weight * loss_seq + self.hparams.ctc_weight * loss_ctc
        else:
            loss = loss_seq
        if self.mode == TrainMode.HOMOGRAPH:
            # When tokenization is used, the length of the words
            # in the original phoneme space is not equal to the tokenized
            # words but the raw data only supplies non-tokenized information
            phns_base, phn_lens_base = (
                batch.phn_raw_encoded if self.phn_tokenize else (None, None)
            )
            homograph_loss = (
                self.hparams.homograph_loss_weight
                * self.hparams.homograph_cost(
                    phns=phns,
                    phn_lens=phn_lens,
                    p_seq=predictions.p_seq,
                    subsequence_phn_start=batch.homograph_phn_start,
                    subsequence_phn_end=batch.homograph_phn_end,
                    phns_base=phns_base,
                    phn_lens_base=phn_lens_base,
                )
            )
            loss += homograph_loss

        # Record losses for posterity
        if self.enable_metrics:
            if stage != sb.Stage.TRAIN:
                self.seq_metrics.append(
                    ids, predictions.p_seq, phns_eos, phn_lens
                )
                self.per_metrics.append(
                    ids,
                    predictions.hyps,
                    phns,
                    None,
                    phn_lens,
                    self.hparams.out_phoneme_decoder,
                )
                if self.mode == TrainMode.HOMOGRAPH:
                    self._add_homograph_metrics(predictions, batch)

        return loss

    def _add_homograph_metrics(self, predictions, batch):
        """Extracts the homograph from the sequence, computes metrics for it
        and registers them

        Arguments
        ---------
        predictions: G2PPredictions
            the predictions (as computed by compute_forward)
        batch: PaddedBatch
            a raw G2P data batch
        """
        phns, phn_lens = batch.phn_encoded
        phns_base, phn_base_lens = (
            batch.phn_raw_encoded if self.phn_tokenize else (None, None)
        )
        ids = batch.id
        (
            p_seq_homograph,
            phns_homograph,
            phn_lens_homograph,
        ) = self.hparams.homograph_extractor(
            phns,
            phn_lens,
            predictions.p_seq,
            subsequence_phn_start=batch.homograph_phn_start,
            subsequence_phn_end=batch.homograph_phn_end,
            phns_base=phns_base,
            phn_base_lens=phn_base_lens,
        )
        hyps_homograph = self.hparams.homograph_extractor.extract_hyps(
            phns_base if phns_base is not None else phns,
            predictions.hyps,
            batch.homograph_phn_start,
            use_base=True,
        )
        self.seq_metrics_homograph.append(
            ids, p_seq_homograph, phns_homograph, phn_lens_homograph
        )
        self.per_metrics_homograph.append(
            ids,
            hyps_homograph,
            phns_homograph,
            None,
            phn_lens_homograph,
            self.hparams.out_phoneme_decoder,
        )
        prediction_labels = self._phonemes_to_label(hyps_homograph)
        phns_homograph_list = undo_padding(phns_homograph, phn_lens_homograph)
        target_labels = self._phonemes_to_label(phns_homograph_list)
        self.classification_metrics_homograph.append(
            ids,
            predictions=prediction_labels,
            targets=target_labels,
            categories=batch.homograph_wordid,
        )

    def _tokens_to_list(self, tokens, token_lens):
        """Converts a tensor of tokens combined with a tensor of lengths to a
        nested list of tokens (more suitable for higher-level operations not
        requiring high performance)

        Arguments
        ---------
        tokens: torch.Tensor
            a tensor of tokens
        token_lens: torch.Tensor
            a tensor of sequence lengths

        Returns
        -------
        result: list
            a batch, represented as a list of sequences, each represented
            as a list
        """
        max_len = tokens.size(1)
        return [
            item[: int(item_len * max_len)]
            for item, item_len in zip(tokens, token_lens)
        ]

    def _phonemes_to_label(self, phns):
        """Converts a batch of phoneme sequences (a single tensor)
        to a list of space-separated phoneme label strings,
        (e.g. ["T AY B L", "B UH K"]), removing any special tokens

        Arguments
        ---------
        phn: sequence
            a batch of phoneme sequences

        Returns
        -------
        result: list
            a list of strings corresponding to the phonemes provided"""
        phn_decoded = self.hparams.out_phoneme_decoder(phns)
        return [" ".join(self._remove_special(item)) for item in phn_decoded]

    def _remove_special(self, phn):
        """Removes any special tokens from the sequence. Special tokens are delimited
        by angle brackets.

        Arguments
        ---------
        phn: list
            a list of phoneme labels

        Returns
        -------
        result: list
            the original list, without any special tokens
        """
        return [token for token in phn if "<" not in token]

    def is_ctc_active(self, stage):
        """Determines whether or not the CTC loss should be enabled.
        It is enabled only if a ctc_lin module has been defined in
        the hyperparameter file, only during training and only for
        the number of epochs determined by the ctc_epochs hyperparameter
        of the corresponding training step.

        Arguments
        ---------
        stage: speechbrain.Stage
            the training stage
        """
        if not self.has_ctc or stage != sb.Stage.TRAIN:
            return False
        current_epoch = self.epoch_counter.current
        return current_epoch <= self.train_step["ctc_epochs"]

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        predictions = self.compute_forward(batch, sb.Stage.TRAIN)
        loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)
        loss.backward()

        if self.check_gradients(loss):
            self.optimizer.step()
        self.optimizer.zero_grad()
        if (
            self.hparams.lr_annealing is not None
            and self.lr_annealing_mode == "step"
        ):
            self.hparams.lr_annealing(self.optimizer)

        return loss.detach()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        predictions = self.compute_forward(batch, stage=stage)
        loss = self.compute_objectives(predictions, batch, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        self.seq_metrics = self.hparams.seq_stats()

        if self.start_epoch is None:
            self.start_epoch = epoch

        if self.enable_metrics:
            if stage != sb.Stage.TRAIN:
                self.per_metrics = self.hparams.per_stats()

            if self.mode == TrainMode.HOMOGRAPH:
                self.seq_metrics_homograph = self.hparams.seq_stats_homograph()
                self.classification_metrics_homograph = (
                    self.hparams.classification_stats_homograph()
                )

                if stage != sb.Stage.TRAIN:
                    self.per_metrics_homograph = (
                        self.hparams.per_stats_homograph()
                    )

        if self.mode == TrainMode.HOMOGRAPH:
            self._set_word_separator()
        self.grapheme_word_separator_idx = self.hparams.grapheme_encoder.lab2ind[
            " "
        ]
        if self.use_word_emb:
            self.modules.word_emb = self.hparams.word_emb().to(self.device)

    def _set_word_separator(self):
        """Determines the word separators to be used"""
        if self.phn_tokenize:
            word_separator_idx = self.hparams.token_space_index
            word_separator_base_idx = self.phoneme_encoder.lab2ind[" "]
        else:
            word_separator_base_idx = (
                word_separator_idx
            ) = self.phoneme_encoder.lab2ind[" "]

        self.hparams.homograph_cost.word_separator = word_separator_idx
        self.hparams.homograph_cost.word_separator_base = (
            word_separator_base_idx
        )
        self.hparams.homograph_extractor.word_separator = word_separator_idx
        self.hparams.homograph_extractor.word_separator_base = (
            word_separator_base_idx
        )

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of a epoch."""
        if stage == sb.Stage.TRAIN:
            self.train_loss = stage_loss
        elif self.enable_metrics:
            per = self.per_metrics.summarize("error_rate")
        ckpt_predicate, ckpt_meta, min_keys = None, {}, None
        if stage == sb.Stage.VALID:
            if (
                self.hparams.lr_annealing is not None
                and self.lr_annealing_mode == "epoch"
            ):
                if (
                    isinstance(
                        self.hparams.lr_annealing,
                        sb.nnet.schedulers.NewBobScheduler,
                    )
                    and self.enable_metrics
                ):
                    old_lr, new_lr = self.hparams.lr_annealing(per)
                elif isinstance(
                    self.hparams.lr_annealing,
                    sb.nnet.schedulers.ReduceLROnPlateau,
                ):
                    old_lr, new_lr = self.hparams.lr_annealing(
                        optim_list=[self.optimizer],
                        current_epoch=epoch,
                        current_loss=self.train_loss,
                    )
                else:
                    old_lr, new_lr = self.hparams.lr_annealing(epoch)
                sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)
            else:
                # No annealing - the LR stays the same
                old_lr = new_lr = self.optimizer.param_groups[0]["lr"]

            stats = {
                "stats_meta": {"epoch": epoch, "lr": old_lr},
                "train_stats": {"loss": self.train_loss},
                "valid_stats": {"loss": stage_loss},
            }
            if self.enable_metrics:
                stats["valid_stats"].update(
                    {
                        "seq_loss": self.seq_metrics.summarize("average"),
                        "PER": per,
                    }
                )

                if self.mode == TrainMode.HOMOGRAPH:
                    per_homograph = self.per_metrics_homograph.summarize(
                        "error_rate"
                    )
                    stats["valid_stats"].update(
                        {
                            "seq_loss_homograph": self.seq_metrics_homograph.summarize(
                                "average"
                            ),
                            "PER_homograph": per_homograph,
                        }
                    )
                    ckpt_meta = {"PER_homograph": per}
                    min_keys = ["PER_homograph"]
                    ckpt_predicate = self._has_homograph_per
                else:
                    ckpt_meta = {"PER": per}
                    min_keys = ["PER"]
                hp.report_result(stats["valid_stats"])

            stats = self._add_stats_prefix(stats)
            self.hparams.train_logger.log_stats(**stats)
            if self.hparams.use_tensorboard:
                self.hparams.tensorboard_train_logger.log_stats(**stats)
                self.save_samples()
            if self.hparams.ckpt_enable and epoch % self.ckpt_frequency == 0:
                self.checkpointer.save_and_keep_only(
                    meta=ckpt_meta,
                    min_keys=min_keys,
                    ckpt_predicate=ckpt_predicate,
                )
            if self.hparams.enable_interim_reports:
                if self.enable_metrics:
                    self._write_reports(epoch, final=False)

        if stage == sb.Stage.TEST:
            test_stats = {"loss": stage_loss}
            if self.enable_metrics:
                test_stats["PER"] = per
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.epoch_counter.current},
                test_stats=test_stats,
            )
            if self.enable_metrics:
                self._write_reports(epoch)

    def _has_homograph_per(self, ckpt):
        """Determines if the provided checkpoint has a homograph PER. Used
        when selecting the best epochs for the homograph loss.

        Arguments
        ---------
        ckpt: speechbrain.utils.checkpoints.Checkpoint
            a checkpoint

        Returns
        -------
        result: bool
            whether it contains a homograph PER"""
        return "PER_homograph" in ckpt.meta

    def _get_interim_report_path(self, epoch, file_path):
        """Determines the path to the interim, per-epoch report

        Arguments
        ---------
        epoch: int
            the epoch number
        file_path: str
            the raw report path
        """
        output_path = os.path.join(
            self.hparams.output_folder,
            "reports",
            self.train_step["name"],
            str(epoch),
        )

        if not os.path.exists(output_path):
            os.makedirs(output_path)
        return os.path.join(output_path, os.path.basename(file_path))

    def _get_report_path(self, epoch, key, final):
        """Determines the path in which to save a given report

        Arguments
        ---------
        epoch: int
            the epoch number
        key: str
            the key within the training step definition in the
            hyperparameter file (e.g. "wer_file")
        final: bool
            whether or not this si the final report. If
            final is false, an epoch number will be inserted into the path

        Arguments
        ---------
        file_name: str
            the report file name
        """
        file_name = self.train_step[key]
        if not final:
            file_name = self._get_interim_report_path(epoch, file_name)
        return file_name

    def _write_reports(self, epoch, final=True):
        """Outputs all reports for a given epoch

        Arguments
        ---------
        epoch: int
            the epoch number

        final: bool
            whether or not the reports are final (i.e.
            after the final epoch)

        Returns
        -------
        file_name: str
            the report file name
        """
        wer_file_name = self._get_report_path(epoch, "wer_file", final)
        self._write_wer_file(wer_file_name)
        if self.mode == TrainMode.HOMOGRAPH:
            homograph_stats_file_name = self._get_report_path(
                epoch, "homograph_stats_file", final
            )
            self._write_homograph_file(homograph_stats_file_name)

    def _write_wer_file(self, file_name):
        """Outputs the Word Error Rate (WER) file

        Arguments
        ---------
        file_name: str
            the report file name
        """
        with open(file_name, "w") as w:
            w.write("\nseq2seq loss stats:\n")
            self.seq_metrics.write_stats(w)
            w.write("\nPER stats:\n")
            self.per_metrics.write_stats(w)
            print("seq2seq, and PER stats written to file", file_name)

    def _write_homograph_file(self, file_name):
        """Outputs the detailed homograph report, detailing the accuracy
        percentage for each homograph, as well as the relative frequencies
        of particular output sequences output by the model

        Arguments
        ---------
        file_name: str
            the report file name
        """
        with open(file_name, "w") as w:
            self.classification_metrics_homograph.write_stats(w)

    def _add_stats_prefix(self, stats):
        """
        Adds a training step prefix to every key in the provided statistics
        dictionary

        Arguments
        ---------
        stats: dict
            a statistics dictionary

        Returns
        ---------
        stats: dict
            a prefixed statistics dictionary
        """
        prefix = self.train_step["name"]
        return {
            stage: {
                f"{prefix}_{key}": value for key, value in stage_stats.items()
            }
            for stage, stage_stats in stats.items()
        }

    @property
    def tb_writer(self):
        """Returns the raw TensorBoard logger writer"""
        return self.hparams.tensorboard_train_logger.writer

    @property
    def tb_global_step(self):
        """Returns the global step number in the Tensorboard writer"""
        global_step = self.hparams.tensorboard_train_logger.global_step
        prefix = self.train_step["name"]
        return global_step["valid"][f"{prefix}_loss"]

    def save_samples(self):
        """Saves attention alignment and text samples to the Tensorboard
        writer"""
        self._save_attention_alignment()
        self._save_text_alignments()

    def _save_text_alignments(self):
        """Saves text predictions aligned with lables (a sample, for progress
        tracking)"""
        if not self.enable_metrics:
            return
        last_batch_sample = self.per_metrics.scores[
            -self.hparams.eval_prediction_sample_size :
        ]
        metrics_by_wer = sorted(
            self.per_metrics.scores, key=lambda item: item["WER"], reverse=True
        )
        worst_sample = metrics_by_wer[
            : self.hparams.eval_prediction_sample_size
        ]
        sample_size = min(
            self.hparams.eval_prediction_sample_size,
            len(self.per_metrics.scores),
        )
        random_sample = np.random.choice(
            self.per_metrics.scores, sample_size, replace=False
        )
        text_alignment_samples = {
            "last_batch": last_batch_sample,
            "worst": worst_sample,
            "random": random_sample,
        }
        prefix = self.train_step["name"]
        for key, sample in text_alignment_samples.items():
            self._save_text_alignment(
                tag=f"valid/{prefix}_{key}", metrics_sample=sample
            )

    def _save_attention_alignment(self):
        """Saves attention alignments"""
        attention = self.last_attn[0]
        if attention.dim() > 2:
            attention = attention[0]
        alignments_max = (
            attention.max(dim=-1)
            .values.max(dim=-1)
            .values.unsqueeze(-1)
            .unsqueeze(-1)
        )
        alignments_output = (
            attention.T.flip(dims=(1,)) / alignments_max
        ).unsqueeze(0)
        prefix = self.train_step["name"]
        self.tb_writer.add_image(
            f"valid/{prefix}_attention_alignments",
            alignments_output,
            self.tb_global_step,
        )

    def _save_text_alignment(self, tag, metrics_sample):
        """Saves a single text sample

        Arguments
        ---------
        tag: str
            the tag - for Tensorboard
        metrics_sample:  list
            List of wer details by utterance,
            see ``speechbrain.utils.edit_distance.wer_details_by_utterance``
            for format. Has to have alignments included.
        """
        with StringIO() as text_alignments_io:
            print_alignments(
                metrics_sample,
                file=text_alignments_io,
                print_header=False,
                sample_separator="\n  ---  \n",
            )
            text_alignments_io.seek(0)
            alignments_sample = text_alignments_io.read()
            alignments_sample_md = f"```\n{alignments_sample}\n```"
        self.tb_writer.add_text(tag, alignments_sample_md, self.tb_global_step)


def sort_data(data, hparams, train_step):
    """Sorts the dataset according to hyperparameter values

    Arguments
    ---------
    data: speechbrain.dataio.dataset.DynamicItemDataset
        the dataset to be sorted
    hparams: dict
        raw hyperparameters
    train_step: dict
        the hyperparameters of the training step

    Returns
    -------
    data: speechbrain.dataio.dataset.DynamicItemDataset
        sorted data
    """
    if hparams["sorting"] == "ascending":
        # we sort training data to speed up training and get better results.
        data = data.filtered_sorted(sort_key="duration")

    elif hparams["sorting"] == "descending":
        data = data.filtered_sorted(sort_key="duration", reverse=True)

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "sorting must be random, ascending or descending"
        )

    sample = train_step.get("sample")
    if sample:
        sample_ids = list(data.data_ids)
        if train_step.get("sample_random"):
            random.shuffle(sample_ids)
        sample_ids = sample_ids[:sample]
        data = FilteredSortedDynamicItemDataset(data, sample_ids)

    return data


def filter_origins(data, hparams):
    """Filters a dataset using a specified list of origins,
    as indicated by the "origin" key in the hyperparameters
    provided

    Arguments
    ---------
    data: speechbrain.dataio.dataset.DynamicItemDataset
        the data to be filtered
    hparams: dict
        the hyperparameters data

    Results
    -------
    data: speechbrain.dataio.dataset.DynamicItemDataset
        the filtered data
    """
    origins = hparams.get("origins")
    if origins and origins != "*":
        origins = set(origins.split(","))
        data = data.filtered_sorted(
            key_test={"origin": lambda origin: origin in origins}
        )
    return data


# TODO: Split this up into smaller functions
def dataio_prep(hparams, train_step=None):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions.

    Arguments
    ---------
    hparams: dict
        the hyperparameters dictionary

    train_step: dict
        the hyperparameters for the training step being executed

    Returns
    -------
    train_data: speechbrain.dataio.dataset.DynamicItemDataset
        the training dataset

    valid_data: speechbrain.dataio.dataset.DynamicItemDataset
        the validation dataset

    test_data: speechbrain.dataio.dataset.DynamicItemDataset
        the test dataset

    phoneme_encoder: speechbrain.dataio.encoder.TextEncoder
        the phoneme encoder
    """

    if not train_step:
        train_step = hparams
    data_folder = hparams["data_folder"]
    data_load = hparams["data_load"]
    # 1. Declarations:
    train_data = data_load(
        train_step["train_data"], replacements={"data_root": data_folder},
    )
    if hparams["sorting"] == "ascending":
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "descending":
        hparams["dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "sorting must be random, ascending or descending"
        )
    is_homograph = (
        TrainMode(train_step.get("mode", TrainMode.NORMAL))
        == TrainMode.HOMOGRAPH
    )

    train_data = sort_data(train_data, hparams, train_step)
    valid_data = data_load(
        train_step["valid_data"], replacements={"data_root": data_folder},
    )
    valid_data = sort_data(valid_data, hparams, train_step)

    test_data = data_load(
        train_step["test_data"], replacements={"data_root": data_folder},
    )
    test_data = sort_data(test_data, hparams, train_step)

    datasets = [train_data, valid_data, test_data]

    phoneme_encoder = hparams["phoneme_encoder"]

    # 2. Define grapheme and phoneme pipelines:
    if hparams.get("char_tokenize"):
        grapheme_pipeline_item = tokenizer_encode_pipeline(
            tokenizer=hparams["grapheme_tokenizer"],
            tokens=hparams["graphemes"],
            takes="char",
            provides_prefix="grapheme",
            wordwise=hparams["char_token_wordwise"],
            space_separated=hparams["phonemes_space_separated"],
            token_space_index=hparams["token_space_index"],
        )
    else:
        grapheme_pipeline_item = grapheme_pipeline(
            graphemes=hparams["graphemes"],
            space_separated=hparams["graphemes_space_separated"],
        )

    if hparams.get("phn_tokenize"):
        phoneme_pipeline_item = tokenizer_encode_pipeline(
            tokenizer=hparams["phoneme_tokenizer"],
            tokens=hparams["phonemes"],
            takes="phn",
            provides_prefix="phn",
            char_map=hparams["phn_char_map"],
            wordwise=hparams["phn_token_wordwise"],
            space_separated=hparams["phonemes_space_separated"],
            token_space_index=hparams["token_space_index"],
        )
        # Ensure the tokenizers are trained
        if "grapheme_tokenizer" in hparams:
            hparams["grapheme_tokenizer"]()
        if "phoneme_tokenizer" in hparams:
            hparams["phoneme_tokenizer"]()
        enable_eos_bos(
            tokens=hparams["phonemes"],
            encoder=phoneme_encoder,
            bos_index=hparams["bos_index"],
            eos_index=hparams["eos_index"],
        )
    else:
        phoneme_pipeline_item = phoneme_pipeline(
            phoneme_encoder=phoneme_encoder,
            space_separated=hparams["phonemes_space_separated"],
        )

    phn_bos_eos_pipeline_item = add_bos_eos(
        tokens=hparams["phonemes"],
        encoder=phoneme_encoder,
        bos_index=hparams["bos_index"],
        eos_index=hparams["eos_index"],
        prefix="phn",
    )
    grapheme_bos_eos_pipeline_item = add_bos_eos(
        tokens=hparams["graphemes"],
        # TODO: Use the grapheme encoder here (this will break some models)
        encoder=phoneme_encoder,
        bos_index=hparams["bos_index"],
        eos_index=hparams["eos_index"],
        prefix="grapheme",
    )
    dynamic_items = [
        grapheme_pipeline_item,
        phoneme_pipeline_item,
        phn_bos_eos_pipeline_item,
        grapheme_bos_eos_pipeline_item,
    ]

    if hparams.get("phn_tokenize"):
        # A raw tokenizer is needed to determine the correct
        # word boundaries from data
        phoneme_raw_pipeline = phoneme_pipeline(
            phoneme_encoder=phoneme_encoder,
            space_separated=hparams["phonemes_space_separated"],
            provides_prefix="phn_raw",
        )
        dynamic_items.append(phoneme_raw_pipeline)

    for dynamic_item in dynamic_items:
        sb.dataio.dataset.add_dynamic_item(datasets, dynamic_item)

    # 3. Set output:
    output_keys = [
        "id",
        "grapheme_encoded",
        "grapheme_encoded_bos",
        "grapheme_encoded_eos",
        "phn_encoded",
        "phn_encoded_eos",
        "phn_encoded_bos",
    ]
    if is_homograph:
        output_keys += [
            "homograph_wordid",
            "homograph_phn_start",
            "homograph_phn_end",
        ]

    if hparams.get("use_word_emb", False):
        output_keys.append("char")
    if (
        hparams.get("phn_tokenize", False)
        and "phn_raw_encoded" not in output_keys
    ):
        output_keys.append("phn_raw_encoded")

    sb.dataio.dataset.set_output_keys(
        datasets, output_keys,
    )
    if "origins" in hparams:
        datasets = [filter_origins(dataset, hparams) for dataset in datasets]
        train_data, valid_data, test_data = datasets

    sample = hparams.get("sample")
    if sample:
        datasets = [filter_origins(dataset, hparams) for dataset in datasets]
    train_data, valid_data, test_data = datasets
    valid_data.data_ids = valid_data.data_ids
    return train_data, valid_data, test_data, phoneme_encoder


def check_language_model(hparams, run_opts):
    """Checks whether or not the language
    model is being used and makes the necessary
    adjustments"""

    if hparams.get("use_language_model"):
        hparams["beam_searcher"] = hparams["beam_searcher_lm"]
        load_dependencies(hparams, run_opts)
    else:
        if "beam_searcher_lm" in hparams:
            del hparams["beam_searcher_lm"]


def load_dependencies(hparams, run_opts):
    """Loads any pre-trained dependencies (e.g. language models)

    Arguments
    ---------
    hparams: dict
        the hyperparamters dictionary
    run_opts: dict
        run options
    """
    deps_pretrainer = hparams.get("deps_pretrainer")
    if deps_pretrainer:
        run_on_main(deps_pretrainer.collect_files)
        deps_pretrainer.load_collected(device=run_opts["device"])


def check_tensorboard(hparams):
    """Checks whether Tensorboard is enabled and initializes the logger if it is

    Arguments
    ---------
    hparams: dict
        the hyperparameter dictionary
    """
    if hparams["use_tensorboard"]:
        from speechbrain.utils.train_logger import TensorboardLogger

        hparams["tensorboard_train_logger"] = TensorboardLogger(
            hparams["tensorboard_logs"]
        )


if __name__ == "__main__":
    # CLI:
    with hp.hyperparameter_optimization(objective_key="PER") as hp_ctx:
        hparams_file, run_opts, overrides = hp_ctx.parse_arguments(sys.argv[1:])

        # Load hyperparameters file with command-line overrides
        with open(hparams_file) as fin:
            hparams = load_hyperpyyaml(fin, overrides)

        # Initialize ddp (useful only for multi-GPU DDP training)
        sb.utils.distributed.ddp_init_group(run_opts)

        check_language_model(hparams, run_opts)
        check_tensorboard(hparams)

        from librispeech_prepare import prepare_librispeech  # noqa
        from tokenizer_prepare import prepare_tokenizer  # noqa

        # Create experiment directory
        sb.create_experiment_directory(
            experiment_directory=hparams["output_folder"],
            hyperparams_to_save=hparams_file,
            overrides=overrides,
        )
        if hparams["build_lexicon"]:
            # multi-gpu (ddp) save data preparation
            run_on_main(
                prepare_librispeech,
                kwargs={
                    "data_folder": hparams["data_folder"],
                    "save_folder": hparams["save_folder"],
                    "create_lexicon": True,
                    "skip_prep": hparams["skip_prep"],
                    "select_n_sentences": hparams.get("select_n_sentences"),
                },
            )
        if hparams.get("char_tokenize") or hparams.get("phn_tokenize"):
            path_keys = [
                "grapheme_tokenizer_output_folder",
                "phoneme_tokenizer_output_folder",
            ]
            paths = [hparams[key] for key in path_keys]
            for path in paths:
                if not os.path.exists(path):
                    os.makedirs(path)
            run_on_main(
                prepare_tokenizer,
                kwargs={
                    "data_folder": hparams["data_folder"],
                    "save_folder": hparams["save_folder"],
                    "phonemes": hparams["phonemes"],
                },
            )
        for train_step in hparams["train_steps"]:
            epochs = train_step["epoch_counter"].limit
            if epochs < 1:
                print(f"Skipping training step: {train_step['name']}")
                continue
            print(f"Running training step: {train_step['name']}")
            # Dataset IO prep: creating Dataset objects and proper encodings for phones
            train_data, valid_data, test_data, phoneme_encoder = dataio_prep(
                hparams, train_step
            )

            # Trainer initialization
            g2p_brain = G2PBrain(
                train_step_name=train_step["name"],
                modules=hparams["modules"],
                opt_class=hparams["opt_class"],
                hparams=hparams,
                run_opts=run_opts,
                checkpointer=hparams["checkpointer"],
            )
            g2p_brain.phoneme_encoder = phoneme_encoder

            # NOTE: This gets modified after the first run and causes a double
            # agument issue
            dataloader_opts = train_step.get(
                "dataloader_opts", hparams.get("dataloader_opts", {})
            )
            if (
                "ckpt_prefix" in dataloader_opts
                and dataloader_opts["ckpt_prefix"] is None
            ):
                del dataloader_opts["ckpt_prefix"]

            train_dataloader_opts = dataloader_opts
            if train_step.get("balance"):
                sampler = BalancingDataSampler(
                    train_data, train_step["balance_on"]
                )
                train_dataloader_opts = dict(dataloader_opts, sampler=sampler)
            start_epoch = train_step["epoch_counter"].current

            # Training/validation loop
            g2p_brain.fit(
                train_step["epoch_counter"],
                train_data,
                valid_data,
                train_loader_kwargs=train_dataloader_opts,
                valid_loader_kwargs=dataloader_opts,
            )

            # Revalidate
            if hparams.get("revalidate"):
                print("Revalidating")
                g2p_brain.evaluate(
                    valid_data,
                    min_key="PER",
                    test_loader_kwargs=dataloader_opts,
                )

            # Test
            # NOTE: Testing will not be re-run if this step has already been completed
            if g2p_brain.start_epoch is not None or hparams.get("retest"):
                g2p_brain.evaluate(
                    test_data,
                    min_key="PER",
                    test_loader_kwargs=dataloader_opts,
                )

            if hparams.get("save_for_pretrained"):
                min_key = (
                    "PER_homograph"
                    if hparams.get("mode") == "homograph"
                    else "PER"
                )
                save_for_pretrained(hparams, min_key=min_key)
