import numpy as np
from typing import List, Dict, Any, Callable, Optional

import paddle
from paddle import nn
import paddle.nn.functional as F
from paddle.optimizer import Optimizer
import paddle.nn.initializer as paddle_init

from paddlets.datasets import TSDataset
from paddlets.models.utils import to_tsdataset
from paddlets.models.anomaly.dl.anomaly_base import AnomalyBaseModel
from paddlets.models.anomaly.dl import utils as U
from paddlets.models.common.callbacks import Callback
from paddlets.logger import raise_if, raise_if_not, raise_log, Logger
from paddlets.utils import manager, param_init

zeros_ = paddle_init.Constant(value=0.)
ones_ = paddle_init.Constant(value=1.)

logger = Logger(__name__)


class moving_avg(paddle.nn.Layer):
    """
    Moving average block to highlight the trend of time series
    """

    def __init__(self, kernel_size, stride):
        super(moving_avg, self).__init__()
        self.kernel_size = kernel_size
        self.avg = paddle.nn.AvgPool1D(
            kernel_size=kernel_size, stride=stride, padding=0)

    def forward(self, x):
        front = x[:, 0:1, :].tile(
            repeat_times=[1, (self.kernel_size - 1) // 2, 1])
        end = x[:, -1:, :].tile(
            repeat_times=[1, (self.kernel_size - 1) // 2, 1])
        x = paddle.concat(x=[front, x, end], axis=1)
        x = self.avg(x.transpose(perm=[0, 2, 1]))
        x = x.transpose(perm=[0, 2, 1])
        return x


class series_decomp(paddle.nn.Layer):
    """
    Series decomposition block
    """

    def __init__(self, kernel_size):
        super(series_decomp, self).__init__()
        self.moving_avg = moving_avg(kernel_size, stride=1)

    def forward(self, x):
        moving_mean = self.moving_avg(x)
        res = x - moving_mean
        return res, moving_mean


class _DLinearModule(paddle.nn.Layer):
    """
    The DLinear implementation based on PaddlePaddle.

    The original article refers to
    Ailing Zeng, Muxi Chen, et al. "Are Transformers Effective for Time Series Forecasting?"
    (https://arxiv.org/pdf/2205.13504.pdf)
    """

    def __init__(self,
                 c_in=7,
                 seq_len=96,
                 pred_len=96,
                 individual=False,
                 pretrain=None):
        super(_DLinearModule, self).__init__()
        self.seq_len = seq_len
        self.pred_len = seq_len
        kernel_size = 25
        self.decompsition = series_decomp(kernel_size)
        self.individual = individual
        self.channels = c_in
        if self.individual:
            self.Linear_Seasonal = paddle.nn.LayerList()
            self.Linear_Trend = paddle.nn.LayerList()
            for i in range(self.channels):
                self.Linear_Seasonal.append(
                    paddle.nn.Linear(
                        in_features=self.seq_len, out_features=self.pred_len))
                self.Linear_Trend.append(
                    paddle.nn.Linear(
                        in_features=self.seq_len, out_features=self.pred_len))
        else:
            self.Linear_Seasonal = paddle.nn.Linear(
                in_features=self.seq_len, out_features=self.pred_len)
            self.Linear_Trend = paddle.nn.Linear(
                in_features=self.seq_len, out_features=self.pred_len)
        self.pretrain = pretrain
        self.init_weight()

    def forward(self, X):
        x = X["observed_cov_numeric"].cast('float32')
        seasonal_init, trend_init = self.decompsition(x)
        seasonal_init, trend_init = seasonal_init.transpose(
            perm=[0, 2, 1]), trend_init.transpose(perm=[0, 2, 1])
        if self.individual:

            seasonal_output = paddle.zeros(
                shape=[
                    seasonal_init.shape[0], seasonal_init.shape[1],
                    self.pred_len
                ],
                dtype=seasonal_init.dtype)

            trend_output = paddle.zeros(
                shape=[
                    trend_init.shape[0], trend_init.shape[1], self.pred_len
                ],
                dtype=trend_init.dtype)
            for i in range(self.channels):
                seasonal_output[:, (i), :] = self.Linear_Seasonal[i](
                    seasonal_init[:, (i), :])
                trend_output[:, (i), :] = self.Linear_Trend[i](trend_init[:, (
                    i), :])
        else:
            seasonal_output = self.Linear_Seasonal(seasonal_init)
            trend_output = self.Linear_Trend(trend_init)
        x = seasonal_output + trend_output
        return x.transpose(
            perm=[0, 2, 1]), X["observed_cov_numeric"].cast('float32')

    def init_weight(self):
        if self.pretrain:
            para_state_dict = paddle.load(self.pretrain)
            model_state_dict = self.state_dict()
            keys = model_state_dict.keys()
            num_params_loaded = 0
            for k in keys:
                if k not in para_state_dict:
                    logger.warning("{} is not in pretrained model".format(k))
                elif list(para_state_dict[k].shape) != list(model_state_dict[k]
                                                            .shape):
                    logger.warning(
                        "[SKIP] Shape of pretrained params {} doesn't match.(Pretrained: {}, Actual: {})"
                        .format(k, para_state_dict[k].shape, model_state_dict[k]
                                .shape))
                else:
                    model_state_dict[k] = para_state_dict[k]
                    num_params_loaded += 1
            self.set_dict(model_state_dict)
            logger.info("There are {}/{} variables loaded into {}.".format(
                num_params_loaded,
                len(model_state_dict), self.__class__.__name__))
        else:
            for layer in self.sublayers():
                if isinstance(layer, nn.Linear):
                    param_init.th_linear_fill(layer)


@manager.MODELS.add_component
class DLinear_AD(AnomalyBaseModel):
    """
    Implementation of PatchTST model.

    Args:
        in_chunk_len(int): The size of the loopback window, i.e., the number of time steps feed to the model.
        out_chunk_len(int): The size of the forecasting horizon, i.e., the number of time steps output by the model.
        generic_architecture(bool, Optional): Boolean value indicating whether the generic architecture of N-BEATS is used. \
                    If not, the interpretable architecture outlined in the paper (consisting of one trend and one seasonality stack \
                    with appropriate waveform generator functions).
        num_stacks(int, Optional): The number of stacks that make up the whole model. Only used if `generic_architecture` is set to `True`.
        num_blocks(Union[int, List[int]], Optional): The number of blocks making up each stack. \
                    If a list is passed, it must have a length equal to `num_stacks` and every entry in that list corresponds to the corresponding stack.\
                    If an integer is passed, every stack will have the same number of blocks.
        num_layers(int, Optional): The number of fully connected layers preceding the final forking layers in each block of every stack. \
                    Only used if `generic_architecture` is set to `True`.
        layer_widths(Union[int, List[int]], Optional): Determines the number of neurons that make up each fully connected layer in each block of every stack. If a list is passed, it must have a length equal to `num_stacks` and every entry in that list corresponds to the layer width of the corresponding stack. If an integer is passed, every stack will have blocks with FC layers of the same width.
        expansion_coefficient_dim(int, Optional): The dimensionality of the waveform generator parameters, also known as expansion coefficients. Only used if `generic_architecture` is set to `True`.
        trend_polynomial_degree(int, Optional): The degree of the polynomial used as waveform generator in trend stacks. Only used if `generic_architecture` is set to `False`.
        skip_chunk_len(int, Optional): Optional, the number of time steps between in_chunk and out_chunk for a single sample. The skip chunk is neither used as a feature (i.e. X) nor a label (i.e. Y) for a single sample. By default it will NOT skip any time steps.
        sampling_stride(int, optional): sampling intervals between two adjacent samples.
        loss_fn(Callable, Optional): loss function.
        optimizer_fn(Callable, Optional): optimizer algorithm.
        optimizer_params(Dict, Optional): optimizer parameters.
        eval_metrics(List[str], Optional): evaluation metrics of model.
        callbacks(List[Callback], Optional): customized callback functions.
        batch_size(int, Optional): number of samples per batch.
        max_epochs(int, Optional): max epochs during training.
        verbose(int, Optional): verbosity mode.
        patience(int, Optional): number of epochs with no improvement after which learning rate wil be reduced.
        seed(int, Optional): global random seed.
    """

    def __init__(self,
                 in_chunk_len: int,
                 out_chunk_len: int=0,
                 c_in: int=3,
                 individual: bool=False,
                 skip_chunk_len: int=0,
                 sampling_stride: int=1,
                 loss_fn: Callable[..., paddle.Tensor]=F.mse_loss,
                 optimizer_fn: Callable[..., Optimizer]=paddle.optimizer.Adam,
                 anomaly_ratio: float=1,
                 threshold: Optional[float]=None,
                 threshold_coeff: float=1.0,
                 threshold_fn: Callable[..., float]=U.get_threshold,
                 anomaly_score_fn: Callable[..., List[float]]=None,
                 pred_adjust: bool=True,
                 pred_adjust_fn: Callable[..., np.ndarray]=U.result_adjust,
                 optimizer_params: Dict[str, Any]=dict(learning_rate=1e-3),
                 eval_metrics: List[str]=[],
                 callbacks: List[Callback]=[],
                 batch_size: int=32,
                 max_epochs: int=100,
                 verbose: int=1,
                 patience: int=10,
                 seed: Optional[int]=None,
                 **kwargs):
        self.c_in = c_in
        self.individual = individual
        self._anomaly_ratio = anomaly_ratio
        self._criterion = paddle.nn.functional.mse_loss

        super(DLinear_AD, self).__init__(
            in_chunk_len=in_chunk_len,
            sampling_stride=sampling_stride,
            loss_fn=loss_fn,
            optimizer_fn=optimizer_fn,
            threshold=threshold,
            threshold_coeff=threshold_coeff,
            threshold_fn=threshold_fn,
            anomaly_score_fn=anomaly_score_fn,
            pred_adjust=pred_adjust,
            pred_adjust_fn=pred_adjust_fn,
            optimizer_params=optimizer_params,
            eval_metrics=eval_metrics,
            callbacks=callbacks,
            batch_size=batch_size,
            max_epochs=max_epochs,
            verbose=verbose,
            patience=patience,
            seed=seed, )

    def _update_fit_params(
            self,
            train_tsdataset: TSDataset,
            valid_tsdataset: Optional[TSDataset]=None) -> Dict[str, Any]:
        """Infer parameters by TSdataset automatically.

        Args:
            train_tsdataset(TSDataset): Train dataset.
            valid_tsdataset(TSDataset|None): Validation dataset.

        Returns:
            Dict[str, Any]: model parameters.
        """
        fit_params = {
            "observed_dim": train_tsdataset.get_observed_cov().data.shape[1]
        }
        return fit_params

    def _init_network(self) -> paddle.nn.Layer:
        """
        Init network.

        Returns:
            paddle.nn.Layer
        """
        return _DLinearModule(
            c_in=self._fit_params["observed_dim"],
            seq_len=self._in_chunk_len,
            pred_len=self._in_chunk_len,
            individual=self.individual)

    def fit(self,
            train_tsdataset: TSDataset,
            valid_tsdataset: Optional[TSDataset]=None,
            to_static_train: bool=False,
            use_amp: bool=False,
            amp_level="O2"):
        """Train a neural network stored in self._network, 
            Using train_dataloader for training data and valid_dataloader for validation.

        Args: 
            train_tsdataset(TSDataset): Train set. 
            valid_tsdataset(TSDataset|None): Eval set, used for early stopping.
        """
        self.use_amp = use_amp
        self.amp_level = amp_level
        self._check_tsdataset(train_tsdataset)
        if valid_tsdataset is not None:
            self._check_tsdataset(valid_tsdataset)
        self._fit_params = self._update_fit_params(train_tsdataset,
                                                   valid_tsdataset)
        train_dataloader, valid_dataloaders = self._init_fit_dataloaders(
            train_tsdataset, valid_tsdataset)
        self._fit(train_dataloader, valid_dataloaders, to_static_train)

        # Get threshold
        if self._threshold is None:
            dataloader, valid_dataloaders = self._init_fit_dataloaders(
                train_tsdataset, valid_tsdataset, shuffle=False)
            self._threshold = self._get_threshold(dataloader, valid_dataloaders)

    @to_tsdataset(scenario="anomaly_label")
    def predict(self, tsdataset: TSDataset, **predict_kwargs) -> TSDataset:
        """Get anomaly label on a batch. the result are output as tsdataset.

        Args:
            tsdataset(TSDataset): Data to be predicted.
            **predict_kwargs: Additional arguments for `_predict`.

        Returns:
            TSDataset.
        """
        boundary = (len(tsdataset._observed_cov.data) - 1)
        dataloader = self._init_predict_dataloader(tsdataset,
                                                   (boundary, boundary))
        anomaly_score = self._get_anomaly_score(dataloader, **predict_kwargs)
        anomaly_score = np.concatenate(anomaly_score, axis=0).reshape(-1)
        anomaly_label = (anomaly_score >= self._threshold) + 0
        # adjust pred 

        return anomaly_label

    def _get_threshold(self,
                       train_dataloader: TSDataset,
                       val_dataloader: Optional[TSDataset]=None):
        """Get the threshold value to judge anomaly.
        
        Args:
            anomaly_score(np.ndarray): 
            
        Returns:
            float: Thresold value.
        """
        raise_if(train_dataloader is None,
                 f" Please pass in train_tsdataset to calculate the threshold.")
        logger.info(f"calculate threshold...")
        if self.use_amp:
            with paddle.amp.auto_cast(
                                level=self.amp_level,
                                enable=True,
                                custom_white_list={
                                    "elementwise_add", "batch_norm", "sync_batch_norm"
                                },
                                custom_black_list={'bilinear_interp_v2'}):
                self._threshold = self._threshold_fn(
                    self._network,
                    train_dataloader,
                    val_dataloader,
                    anomaly_ratio=self._anomaly_ratio,
                    criterion=self._criterion)
        else:
            self._threshold = self._threshold_fn(
                self._network,
                train_dataloader,
                val_dataloader,
                anomaly_ratio=self._anomaly_ratio,
                criterion=self._criterion)
        logger.info(f"threshold is {self._threshold}")
        return self._threshold

    def _get_loss(self, y_pred: paddle.Tensor,
                  y_true: paddle.Tensor) -> np.ndarray:
        """Get the loss for anomaly label and anomaly score.

        Note:
            This function could be overrided by the subclass if necessary.

        Args:
            y_pred(paddle.Tensor): Estimated feature values.
            y_true(paddle.Tensor): Ground truth (correct) feature values.

        Returns:
            np.ndarray.

        """
        anomaly_criterion = paddle.nn.functional.mse_loss
        loss = paddle.mean(
            x=anomaly_criterion(
                y_true, y_pred, reduction='none'), axis=-1)
        return loss.numpy()
