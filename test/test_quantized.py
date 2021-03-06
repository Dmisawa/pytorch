from __future__ import absolute_import, division, print_function, unicode_literals

import torch
import torch.jit
import numpy as np
import unittest
from common_utils import TEST_WITH_UBSAN, TestCase, run_tests, skipIfNotRegistered
import torch.nn.functional as F


def canonical(graph):
    return str(torch._C._jit_pass_canonicalize(graph))


def _quantize(x, scale, zero_point, qmin=0, qmax=255, qtype=np.uint8):
    """Quantizes a numpy array."""
    qx = np.round(x / scale + zero_point)
    qx = np.clip(qx, qmin, qmax).astype(qtype)
    return qx


def _dequantize(qx, scale, zero_point):
    """Dequantizes a numpy array."""
    x = (qx.astype(np.float) - zero_point) * scale
    return x


def _requantize(x, multiplier, zero_point, qmin=0, qmax=255, qtype=np.uint8):
    """Requantizes a numpy array, i.e., intermediate int32 or int16 values are
    converted back to given type"""
    qx = (x * multiplier).round() + zero_point
    qx = np.clip(qx, qmin, qmax).astype(qtype)
    return qx


# Make sure we won't have overflows from vpmaddubsw instruction used in FBGEMM.
# On the current Intel x86 architecture, we need to utilize vpmaddubsw instruction
# for the 8-bit int multiplication. This instruction vertically multiplies each
# unsigned 8-bit integer from a with the corresponding signed 8-bit integer from
# b, producing intermediate signed 16-bit integers. This function modifies the
# weights to eliminate the overflow on the signed 16-bit integers.
def avoid_vpmaddubsw_overflow_linear(
    batch_size, input_channels, output_channels, X, X_min, X_max, W, W_min, W_max
):
    for i, j in np.ndindex((batch_size, output_channels)):
        for k in range(0, input_channels // 2 * 2, 2):
            x0 = X[i, k] - X_min
            x1 = X[i, k + 1] - X_min
            w0 = W[j, k] - 128 - W_min
            w1 = W[j, k + 1] - 128 - W_min
            if x0 * w0 + x1 * w1 < -(1 << 15):
                w1_adjusted = (-(1 << 15) - float(x0) * w0) / x1
                W[j, k + 1] = int(w1_adjusted) + 128 + W_min
            elif x0 * w0 + x1 * w1 > (1 << 15) - 1:
                w1_adjusted = ((1 << 15) - 1 - float(x0) * w0) / x1
                W[j, k + 1] = int(w1_adjusted) + 128 + W_min

    # Go through the same loop again to double check we don't have any overflow
    for i, j in np.ndindex((batch_size, output_channels)):
        for k in range(0, input_channels // 2 * 2, 2):
            x0 = X[i, k] - X_min
            x1 = X[i, k + 1] - X_min
            w0 = W[j, k] - 128 - W_min
            w1 = W[j, k + 1] - 128 - W_min
            assert -(1 << 15) <= x0 * w0 + x1 * w1 < (1 << 15)


# Reference quantized Linear operator
def qlinear_ref(X_q, X_scale, X_zp, W_q, W_scale, W_zp, b_q, Y_scale, Y_zp):
    row_offsets_ref = X_q.sum(axis=1).astype(np.int32).reshape((-1, 1))
    col_offsets_ref = W_q.sum(axis=1).astype(np.int32).reshape((1, -1))
    assert X_q.ndim == 2
    batch_size, input_channels = X_q.shape
    Prod_XqWq_ref = (
        np.matmul(X_q.astype(np.int32), W_q.astype(np.int32).T)
        - W_zp * row_offsets_ref
        - X_zp * col_offsets_ref
        + input_channels * X_zp * W_zp
    )
    Y_q_ref = _quantize(Prod_XqWq_ref + b_q, Y_scale / (X_scale * W_scale), Y_zp)
    return Y_q_ref


@skipIfNotRegistered("Relu_ENGINE_FBGEMM",
                     "fbgemm-based Caffe2 ops are not linked")
class TestQuantized(TestCase):
    def test_relu(self):
        a = (torch.tensor([4, 6, 1, 10], dtype=torch.uint8), 0.01, 5)
        r = torch.ops.c10.quantized_relu(a)
        np.testing.assert_equal(
            r[0].numpy(), torch.tensor([5, 6, 5, 10], dtype=torch.uint8).numpy()
        )
        np.testing.assert_almost_equal(0.01, r[1])
        self.assertEqual(5, r[2])

    def test_quantize(self):
        a = (torch.tensor([4, 6, 1, 10], dtype=torch.uint8), 0.01, 5)
        r = torch.ops.c10.dequantize(a)
        np.testing.assert_almost_equal(r.numpy(), [-0.01, 0.01, -0.04, 0.05])
        # default args
        q_def = torch.ops.c10.quantize(r)
        # specified
        q = torch.ops.c10.quantize(r, scale=0.01, zero_point=5)
        np.testing.assert_equal(q[0].numpy(), a[0].numpy())
        np.testing.assert_almost_equal(q[1], a[1])
        self.assertEqual(q[2], a[2])

    def test_script(self):
        @torch.jit.script
        def foo(x):
            # type: (Tuple[Tensor, float, int]) -> Tuple[Tensor, float, int]
            return torch.ops.c10.quantized_relu(x)

        self.assertExpectedInline(
            canonical(foo.graph),
            """\
graph(%x : (Tensor, float, int)):
  %1 : (Tensor, float, int) = c10::quantized_relu(%x)
  return (%1)
""",
        )


class TestQuantizedOps(unittest.TestCase):
    """Tests the correctness of the quantized::relu op."""

    def test_qrelu(self):
        relu = torch.ops.quantized.relu

        X = torch.arange(-5, 5, dtype=torch.float)
        scale = 2.0
        zero_point = 1
        qX = X.quantize_linear(scale=scale, zero_point=zero_point, dtype=torch.quint8)

        Y = X.numpy().copy()
        Y[Y < 0] = 0
        qY = _quantize(Y, scale, zero_point)
        qY_hat = relu(qX)
        np.testing.assert_equal(qY, qY_hat.int_repr())

    """Tests the correctness of the add and add_relu op."""
    def test_qadd_relu_same_qparams(self):
        add_relu = torch.ops.quantized.add_relu
        add = torch.ops.quantized.add

        A = torch.arange(-25, 25, dtype=torch.float)
        B = torch.arange(-25, 25, dtype=torch.float)
        scale = 2.0
        zero_point = 127
        qA = A.quantize_linear(scale=scale, zero_point=zero_point, dtype=torch.quint8)
        qB = A.quantize_linear(scale=scale, zero_point=zero_point, dtype=torch.quint8)

        # Add ReLU ground truth
        C = (qA.dequantize() + qB.dequantize()).numpy()
        qC = _quantize(C, scale, zero_point)
        qC_hat = add(qA, qB, scale=scale, zero_point=zero_point)
        np.testing.assert_equal(qC, qC_hat.int_repr(),
                                "Quantized addition failed.")

        # Add + ReLU ground truth
        Crelu = C.copy()
        Crelu[C < 0] = 0
        qCrelu = _quantize(Crelu, scale, zero_point)
        qCrelu_hat = add_relu(qA, qB, scale=scale, zero_point=zero_point)
        np.testing.assert_equal(qCrelu, qCrelu_hat.int_repr(),
                                "Quantized addition with ReLU failed.")

    """Tests the correctness of the add and add_relu op."""
    def test_qadd_relu_different_qparams(self):
        add_relu = torch.ops.quantized.add_relu
        add = torch.ops.quantized.add

        A = torch.arange(-25, 25, dtype=torch.float)
        B = torch.arange(-25, 25, dtype=torch.float)
        scale_A = 3.0
        zero_point_A = 7
        scale_B = 5.0
        zero_point_B = 127

        scale_C = 0.5
        zero_point_C = 5

        qA = A.quantize_linear(scale=scale_A, zero_point=zero_point_A, dtype=torch.quint8)
        qB = A.quantize_linear(scale=scale_B, zero_point=zero_point_B, dtype=torch.quint8)

        # Add ground truth
        C = (qA.dequantize() + qB.dequantize()).numpy()
        qC = _quantize(C, scale_C, zero_point_C)
        qC_hat = add(qA, qB, scale=scale_C, zero_point=zero_point_C)
        np.testing.assert_equal(qC, qC_hat.int_repr(),
                                "Quantized addition failed.")

        # Add + ReLU ground truth
        Crelu = C.copy()
        Crelu[C < 0] = 0
        qCrelu = _quantize(Crelu, scale_C, zero_point_C)
        qCrelu_hat = add_relu(qA, qB, scale=scale_C, zero_point=zero_point_C)
        np.testing.assert_equal(qCrelu, qCrelu_hat.int_repr(),
                                "Quantized addition with ReLU failed.")


@unittest.skipIf(
    TEST_WITH_UBSAN or not torch.fbgemm_is_cpu_supported(),
    " Quantized Linear requires FBGEMM. FBGEMM does not play"
    " well with UBSAN at the moment, so we skip the test if"
    " we are in a UBSAN environment.",
)
class TestQuantizedLinear(unittest.TestCase):
    """Tests the correctness of the quantized::fbgemm_linear op."""

    def test_qlinear(self):
        qlinear_prepack = torch.ops.quantized.fbgemm_linear_prepack
        qlinear = torch.ops.quantized.fbgemm_linear

        batch_size = 4
        input_channels = 16
        output_channels = 8

        X_scale = 1.5
        X_zp = 5
        X_value_min = 0
        X_value_max = 225
        X_q0 = np.round(
            np.random.rand(batch_size, input_channels) * (X_value_max - X_value_min)
            + X_value_min
        ).astype(np.uint8)

        W_scale = 0.4
        W_zp = 2
        W_value_min = -128
        W_value_max = 127
        W_q0 = np.round(
            np.random.rand(output_channels, input_channels)
            * (W_value_max - W_value_min)
            + W_value_min
        ).astype(np.int8)

        b_value_min = -10
        b_value_max = 10
        b_q0 = np.round(
            np.random.rand(output_channels) * (b_value_max - b_value_min) + b_value_min
        ).astype(np.int32)

        avoid_vpmaddubsw_overflow_linear(
            batch_size,
            input_channels,
            output_channels,
            X_q0,
            X_value_min,
            X_value_max,
            W_q0,
            W_value_min,
            W_value_max,
        )

        X = torch.from_numpy(_dequantize(X_q0, X_scale, X_zp)).to(dtype=torch.float)
        W = torch.from_numpy(_dequantize(W_q0, W_scale, W_zp)).to(dtype=torch.float)
        b = torch.from_numpy(_dequantize(b_q0, X_scale * W_scale, 0)).to(dtype=torch.float)

        X_q = X.quantize_linear(scale=X_scale, zero_point=X_zp, dtype=torch.quint8)
        W_q = W.quantize_linear(scale=W_scale, zero_point=W_zp, dtype=torch.qint8)
        b_q = b.quantize_linear(scale=X_scale * W_scale, zero_point=0, dtype=torch.qint32)

        # Compare X_scale * W_scale * input_channels * X_value_max * W_value_max with
        # Y_scale * 255 (max for uint8).
        Y_scale = 125.1234
        Y_zp = 5

        # Reference quantized Linear operator
        Y_q_ref = qlinear_ref(X_q0, X_scale, X_zp, W_q0, W_scale, W_zp, b_q0, Y_scale, Y_zp)

        # Weight prepacking operator for quantized Linear
        W_prepack = qlinear_prepack(W_q)
        # Quantized Linear operator with prepacked weight
        Y_q = qlinear(X_q, W_prepack, b_q, Y_scale, Y_zp)

        # Y_q_ref_real = _dequantize(Y_q_ref, Y_scale, Y_zp)
        # Y_q_real = Y_q.dequantize()

        # Assert equal
        np.testing.assert_equal(Y_q_ref, Y_q.int_repr().numpy())

        # Reference quantized result from PyTorch Linear operator
        W_fp32 = W_q.dequantize().to(dtype=torch.float)
        X_fp32 = X_q.dequantize().to(dtype=torch.float)
        b_fp32 = b_q.dequantize().to(dtype=torch.float)
        Y_fp32_ref = F.linear(X_fp32, W_fp32, b_fp32)
        Y_q_ref2 = Y_fp32_ref.quantize_linear(Y_scale, Y_zp, torch.quint8)

        # Assert equal
        np.testing.assert_equal(Y_q_ref2.int_repr().numpy(), Y_q.int_repr().numpy())


    """Tests the correctness of the quantized::fbgemm_linear_relu op."""
    def test_qlinear_relu(self):
        qlinear_prepack = torch.ops.quantized.fbgemm_linear_prepack
        qlinear_relu = torch.ops.quantized.fbgemm_linear_relu

        batch_size = 4
        input_channels = 16
        output_channels = 8

        X_scale = 1.5
        X_zp = 5
        X_value_min = 0
        X_value_max = 225
        X_q0 = np.round(
            np.random.rand(batch_size, input_channels) * (X_value_max - X_value_min)
            + X_value_min
        ).astype(np.uint8)

        W_scale = 0.4
        W_zp = 2
        W_value_min = -128
        W_value_max = 127
        W_q0 = np.round(
            np.random.rand(output_channels, input_channels)
            * (W_value_max - W_value_min)
            + W_value_min
        ).astype(np.int8)

        b_value_min = -10
        b_value_max = 10
        b_q0 = np.round(
            np.random.rand(output_channels) * (b_value_max - b_value_min) + b_value_min
        ).astype(np.int32)

        avoid_vpmaddubsw_overflow_linear(
            batch_size,
            input_channels,
            output_channels,
            X_q0,
            X_value_min,
            X_value_max,
            W_q0,
            W_value_min,
            W_value_max,
        )

        X = torch.from_numpy(_dequantize(X_q0, X_scale, X_zp)).to(dtype=torch.float)
        W = torch.from_numpy(_dequantize(W_q0, W_scale, W_zp)).to(dtype=torch.float)
        b = torch.from_numpy(_dequantize(b_q0, X_scale * W_scale, 0)).to(dtype=torch.float)

        X_q = X.quantize_linear(scale=X_scale, zero_point=X_zp, dtype=torch.quint8)
        W_q = W.quantize_linear(scale=W_scale, zero_point=W_zp, dtype=torch.qint8)
        b_q = b.quantize_linear(scale=X_scale * W_scale, zero_point=0, dtype=torch.qint32)

        # Compare X_scale * W_scale * input_channels * X_value_max * W_value_max with
        # Y_scale * 255 (max for uint8).
        Y_scale = 125.1234
        Y_zp = 5

        # Reference quantized Linear operator
        Y_q_ref = qlinear_ref(X_q0, X_scale, X_zp, W_q0, W_scale, W_zp, b_q0, Y_scale, Y_zp)
        Y_q_ref[Y_q_ref < Y_zp] = Y_zp

        # Weight prepacking operator for quantized Linear
        W_prepack = qlinear_prepack(W_q)
        # Quantized Linear operator with prepacked weight
        Y_q = qlinear_relu(X_q, W_prepack, b_q, Y_scale, Y_zp)

        # Y_q_ref_real = _dequantize(Y_q_ref, Y_scale, Y_zp)
        # Y_q_real = Y_q.dequantize()

        # Assert equal
        np.testing.assert_equal(Y_q_ref, Y_q.int_repr().numpy())

        # Reference quantized result from PyTorch Linear operator
        W_fp32 = W_q.dequantize().to(dtype=torch.float)
        X_fp32 = X_q.dequantize().to(dtype=torch.float)
        b_fp32 = b_q.dequantize().to(dtype=torch.float)
        Y_fp32_ref = F.linear(X_fp32, W_fp32, b_fp32)
        Y_fp32_ref[Y_fp32_ref < 0.0] = 0.0
        Y_q_ref2 = Y_fp32_ref.quantize_linear(Y_scale, Y_zp, torch.quint8)

        # Assert equal
        np.testing.assert_equal(Y_q_ref2.int_repr().numpy(), Y_q.int_repr().numpy())


@unittest.skipIf(
    TEST_WITH_UBSAN or not torch.fbgemm_is_cpu_supported(),
    " Quantized convolution requires FBGEMM. FBGEMM does not play"
    " well with UBSAN at the moment, so we skip the test if"
    " we are in a UBSAN environment.",
)
class TestQuantizedConv(unittest.TestCase):
    """Tests the correctness of quantized convolution op."""
    def test_qconv(self):

        qconv = torch.ops.quantized.fbgemm_conv2d
        qconv_prepack = torch.ops.quantized.fbgemm_conv_prepack

        # N
        batch_size = 1
        # C
        input_channels = 16
        # H, W
        height = width = 24
        # K
        output_channels = 8

        kernel_h = kernel_w = 3
        stride_h = stride_w = 1
        padding_h = padding_w = 1
        dilation_h = dilation_w = 1
        groups = 1

        W_value_min = 0
        W_value_max = 5
        # We use small values to avoid overflow.
        # (the operator expects them in the format (output_channels, input_channels/groups, kernel_h, kernel_w))

        W_init = torch.randint(
            W_value_min,
            W_value_max,
            (output_channels, int(input_channels / groups), kernel_h, kernel_w),
        )

        b_init = torch.randint(0, 10, (output_channels,))

        # Existing floating point conv operator
        conv_op = torch.nn.Conv2d(
            input_channels,
            output_channels,
            (kernel_h, kernel_w),
            (stride_h, stride_w),
            (padding_h, padding_w),
            (dilation_h, dilation_w),
            groups,
        )

        # assign the weights
        conv_op.weight = torch.nn.Parameter(
            W_init.to(dtype=torch.float), requires_grad=False
        )
        conv_op.bias = torch.nn.Parameter(
            b_init.to(dtype=torch.float), requires_grad=False
        )

        X_value_min = 0
        X_value_max = 4
        X_init = torch.randint(
            X_value_min, X_value_max, (batch_size, input_channels, height, width)
        )

        # run on an input tensor
        result_ref = conv_op(X_init.to(dtype=torch.float))

        # reformat X_init and W_init in the required format by conv operator
        # NCHW -> NHWC
        X_NHWC = X_init.permute([0, 2, 3, 1]).contiguous()
        # KCRS -> RSCK
        W_RSCK = W_init.permute([2, 3, 1, 0]).contiguous()

        X_scale = 1.5
        # Currently only 0 as zero point is supported.
        X_zero_point = 0
        X = X_scale * (X_NHWC - X_zero_point).to(dtype=torch.float)

        W_scale = 2.5
        W_zero_point = 0
        W = W_scale * (W_RSCK - W_zero_point).to(dtype=torch.float)

        X_q = X.quantize_linear(scale=X_scale, zero_point=X_zero_point, dtype=torch.quint8)
        W_q = W.quantize_linear(scale=W_scale, zero_point=W_zero_point, dtype=torch.quint8)
        b_q = b_init.to(dtype=torch.int32)

        W_prepack = qconv_prepack(W_q, groups)
        Y_scale = 7.3
        Y_zero_point = 5

        Y_q = qconv(
            X_q,
            W_prepack,
            b_q,
            [1, 1],  # stride
            [1, 1],  # padding
            [1, 1],  # dilation
            [0, 0],  # output_padding
            1,  # groups
            Y_scale,
            Y_zero_point,
        )

        result_NHWK = result_ref.permute([0, 2, 3, 1])
        result_q = _requantize(
            result_NHWK.numpy(), X_scale * W_scale / Y_scale, Y_zero_point
        )

        # Make sure the results match
        np.testing.assert_equal(result_q, Y_q.int_repr().numpy())


if __name__ == "__main__":
    run_tests()
