# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

import logging

# Standard library imports
import multiprocessing
from typing import List, Optional

import mxnet as mx

# Third-party imports
import numpy as np

from gluonts.core.component import validated

# First-party imports
from gluonts.dataset.common import Dataset, ListDataset
from gluonts.dataset.stat import calculate_dataset_statistics
from gluonts.model.seq2seq._forking_estimator import ForkingSeq2SeqEstimator
from gluonts.mx.block.decoder import ForkingMLPDecoder
from gluonts.mx.block.encoder import (
    HierarchicalCausalConv1DEncoder,
    RNNEncoder,
)
from gluonts.mx.block.quantile_output import QuantileOutput
from gluonts.mx.trainer import Trainer


class MQCNNEstimator(ForkingSeq2SeqEstimator):
    """
    An :class:`MQDNNEstimator` with a Convolutional Neural Network (CNN) as an
    encoder and a multi-quantile MLP as a decoder. Implements the MQ-CNN Forecaster, proposed in [WTN+17]_.

    Parameters
    ----------
    freq
        Time granularity of the data.
    prediction_length
        Length of the prediction, also known as 'horizon'.
    context_length
        Number of time units that condition the predictions, also known as 'lookback period'.
        (default: 4 * prediction_length)
    use_feat_dynamic_real
        Whether to use the ``feat_dynamic_real`` field from the data. (default: False)
        Automatically inferred when creating the MQCNNEstimator with the `from_inputs` class method.
    use_feat_static_cat:
        Whether to use the ``feat_static_cat`` field from the data. (default: False)
        Automatically inferred when creating the MQCNNEstimator with the `from_inputs` class method.
    cardinality:
        Number of values of each categorical feature.
        This must be set if ``use_feat_static_cat == True`` (default: None)
        Automatically inferred when creating the MQCNNEstimator with the `from_inputs` class method.
    embedding_dimension:
        Dimension of the embeddings for categorical features. (default: [min(50, (cat+1)//2) for cat in cardinality])
    add_time_feature
        Adds a set of time features. (default: False)
    add_age_feature
        Adds an age feature. (default: False)
        The age feature starts with a small value at the start of the time series and grows over time.
    enable_decoder_dynamic_feature
        Whether the decoder should also be provided with the dynamic features (``age``, ``time``
        and ``feat_dynamic_real`` if enabled respectively). (default: True)
        It makes sense to disable this, if you dont have ``feat_dynamic_real`` for the prediction range.
    seed
        Will set the specified int seed for numpy anc MXNet if specified. (default: None)
    decoder_mlp_dim_seq
        The dimensionalities of the Multi Layer Perceptron layers of the decoder.
        (default: [30])
    channels_seq
        The number of channels (i.e. filters or convolutions) for each layer of the HierarchicalCausalConv1DEncoder.
        More channels usually correspond to better performance and larger network size.
        (default: [30, 30, 30])
    dilation_seq
        The dilation of the convolutions in each layer of the HierarchicalCausalConv1DEncoder.
        Greater numbers correspond to a greater receptive field of the network, which is usually
        better with longer context_length. (Same length as channels_seq) (default: [1, 3, 5])
    kernel_size_seq
        The kernel sizes (i.e. window size) of the convolutions in each layer of the HierarchicalCausalConv1DEncoder.
        (Same length as channels_seq) (default: [7, 3, 3])
    use_residual
        Whether the hierarchical encoder should additionally pass the unaltered
        past target to the decoder. (default: True)
    quantiles
        The list of quantiles that will be optimized for, and predicted by, the model.
        Optimizing for more quantiles than are of direct interest to you can result
        in improved performance due to a regularizing effect.
        (default: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    trainer
        The GluonTS trainer to use for training. (default: Trainer())
    scaling
        Whether to automatically scale the target values. (default: False)
    """

    @validated()
    def __init__(
        self,
        freq: str,
        prediction_length: int,
        sampling: bool = True,
        distr_output: DistributionOutput = GaussianOutput(),
        context_length: Optional[int] = None,
        use_feat_dynamic_real: bool = False,
        use_feat_static_cat: bool = False,
        cardinality: List[int] = None,
        embedding_dimension: List[int] = None,
        add_time_feature: bool = False,
        add_age_feature: bool = False,
        enable_decoder_dynamic_feature: bool = False,
        seed: Optional[int] = None,
        decoder_mlp_dim_seq: Optional[List[int]] = None,
        channels_seq: Optional[List[int]] = None,
        dilation_seq: Optional[List[int]] = None,
        kernel_size_seq: Optional[List[int]] = None,
        use_residual: bool = True,
        quantiles: Optional[List[float]] = None,
        trainer: Trainer = Trainer(),
        scaling: bool = False,
    ) -> None:

        assert (
            prediction_length > 0
        ), f"Invalid prediction length: {prediction_length}."
        assert decoder_mlp_dim_seq is None or all(
            d > 0 for d in decoder_mlp_dim_seq
        ), "Elements of `mlp_hidden_dimension_seq` should be > 0"
        assert channels_seq is None or all(
            d > 0 for d in channels_seq
        ), "Elements of `channels_seq` should be > 0"
        assert dilation_seq is None or all(
            d > 0 for d in dilation_seq
        ), "Elements of `dilation_seq` should be > 0"
        # TODO: add support for kernel size=1
        assert kernel_size_seq is None or all(
            d > 1 for d in kernel_size_seq
        ), "Elements of `kernel_size_seq` should be > 0"
        assert quantiles is None or all(
            0 <= d <= 1 for d in quantiles
        ), "Elements of `quantiles` should be >= 0 and <= 1"

        self.decoder_mlp_dim_seq = (
            decoder_mlp_dim_seq if decoder_mlp_dim_seq is not None else [30]
        )
        self.channels_seq = (
            channels_seq if channels_seq is not None else [30, 30, 30]
        )
        self.dilation_seq = (
            dilation_seq if dilation_seq is not None else [1, 3, 5]
        )
        self.kernel_size_seq = (
            kernel_size_seq if kernel_size_seq is not None else [7, 3, 3]
        )
        self.quantiles = (
            quantiles
            if quantiles is not None
            else [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        )

        assert (
            len(self.channels_seq)
            == len(self.dilation_seq)
            == len(self.kernel_size_seq)
        ), (
            f"mismatch CNN configurations: {len(self.channels_seq)} vs. "
            f"{len(self.dilation_seq)} vs. {len(self.kernel_size_seq)}"
        )

        if seed:
            np.random.seed(seed)
            mx.random.seed(seed)

        # `use_static_feat` and `use_dynamic_feat` always True because network
        # always receives input; either from the input data or constants
        encoder = HierarchicalCausalConv1DEncoder(
            dilation_seq=self.dilation_seq,
            kernel_size_seq=self.kernel_size_seq,
            channels_seq=self.channels_seq,
            use_residual=use_residual,
            use_static_feat=True,
            use_dynamic_feat=True,
            prefix="encoder_",
        )

        decoder = ForkingMLPDecoder(
            dec_len=prediction_length,
            final_dim=self.decoder_mlp_dim_seq[-1],
            hidden_dimension_sequence=self.decoder_mlp_dim_seq[:-1],
            prefix="decoder_",
        )

        quantile_output = QuantileOutput(self.quantiles)
        self.sampling = sampling
        self.distr_output = distr_output

        super().__init__(
            encoder=encoder,
            decoder=decoder,
            quantile_output=quantile_output,
            freq=freq,
            prediction_length=prediction_length,
            context_length=context_length,
            use_feat_dynamic_real=use_feat_dynamic_real,
            use_feat_static_cat=use_feat_static_cat,
            enable_decoder_dynamic_feature=enable_decoder_dynamic_feature,
            cardinality=cardinality,
            embedding_dimension=embedding_dimension,
            add_time_feature=add_time_feature,
            add_age_feature=add_age_feature,
            trainer=trainer,
            scaling=scaling,
        )

    @classmethod
    def derive_auto_fields(cls, train_iter):
        stats = calculate_dataset_statistics(train_iter)

        auto_fields = {
            "use_feat_dynamic_real": stats.num_feat_dynamic_real > 0,
            "use_feat_static_cat": bool(stats.feat_static_cat),
            "cardinality": [len(cats) for cats in stats.feat_static_cat],
        }

        logger = logging.getLogger(__name__)
        logger.info(
            f"gluonts[from_inputs]: use_feat_dynamic_real set to "
            f"'{auto_fields['use_feat_dynamic_real']}', and use use_feat_static_cat to "
            f"'{auto_fields['use_feat_static_cat']}' with cardinality of '{auto_fields['cardinality']}'"
        )

        return auto_fields


class MQRNNEstimator(ForkingSeq2SeqEstimator):
    """
    An :class:`MQDNNEstimator` with a Recurrent Neural Network (RNN) as an
    encoder and a multi-quantile MLP as a decoder. Implements the MQ-RNN Forecaster, proposed in [WTN+17]_.
    """

    @validated()
    def __init__(
        self,
        prediction_length: int,
        freq: str,
        context_length: Optional[int] = None,
        decoder_mlp_dim_seq: List[int] = None,
        trainer: Trainer = Trainer(),
        quantiles: List[float] = None,
        scaling: bool = True,
    ) -> None:

        assert (
            prediction_length > 0
        ), f"Invalid prediction length: {prediction_length}."
        assert decoder_mlp_dim_seq is None or all(
            d > 0 for d in decoder_mlp_dim_seq
        ), "Elements of `mlp_hidden_dimension_seq` should be > 0"
        assert quantiles is None or all(
            0 <= d <= 1 for d in quantiles
        ), "Elements of `quantiles` should be >= 0 and <= 1"

        self.decoder_mlp_dim_seq = (
            decoder_mlp_dim_seq if decoder_mlp_dim_seq is not None else [30]
        )
        self.quantiles = (
            quantiles
            if quantiles is not None
            else [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        )

        # `use_static_feat` and `use_dynamic_feat` always True because network
        # always receives input; either from the input data or constants
        encoder = RNNEncoder(
            mode="gru",
            hidden_size=50,
            num_layers=1,
            bidirectional=True,
            prefix="encoder_",
            use_static_feat=True,
            use_dynamic_feat=True,
        )

        decoder = ForkingMLPDecoder(
            dec_len=prediction_length,
            final_dim=self.decoder_mlp_dim_seq[-1],
            hidden_dimension_sequence=self.decoder_mlp_dim_seq[:-1],
            prefix="decoder_",
        )

        quantile_output = QuantileOutput(self.quantiles)

        super().__init__(
            encoder=encoder,
            decoder=decoder,
            quantile_output=quantile_output,
            freq=freq,
            prediction_length=prediction_length,
            context_length=context_length,
            trainer=trainer,
            scaling=scaling,
        )
