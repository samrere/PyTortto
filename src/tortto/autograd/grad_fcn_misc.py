from tortto import np, cp, cparray
from .function import *
from .helper import *


class Split(Function):  # keep input _version: True
    @staticmethod
    def forward(ctx, *inputs, **params):
        xt0, = inputs
        xd0 = xt0.data
        dim = params['dim']
        split_size_or_sections = params['split_size_or_sections']
        dim_size = xd0.shape[dim]
        if dim < 0:
            dim += xd0.ndim
        if split_size_or_sections.__class__ is int:
            split_size = split_size_or_sections
            ytn = tuple(
                build_links(
                    xd0[
                        tuple(
                            slice(None) if i != dim else slice(j, j + split_size) for i in range(xd0.ndim)
                        )
                    ],
                    grad_fn=ctx,
                    _output_idx=j // split_size
                )
                for j in range(0, dim_size, split_size)
            )
        else:
            sections = split_size_or_sections
            if sum(sections) != dim_size:
                raise RuntimeError(f"split_with_sizes expects split_sizes to sum exactly to {dim_size} "
                                   f"(input tensor's size at dimension {dim}), but got split_sizes={sections}")
            sum_sections = np.cumsum(split_size_or_sections)
            ytn = tuple(
                build_links(
                    xd0[
                        tuple(
                            slice(None) if i != dim else slice(sum_sections[j] - sec, sum_sections[j]) for i in
                            range(xd0.ndim)
                        )
                    ],
                    grad_fn=ctx,
                    _output_idx=j
                )
                for j, sec in enumerate(sections)
            )
        ctx.save_for_backward(xt0)
        ctx.params['output_shapes'] = tuple(yt.shape for yt in ytn)
        return ytn

    @staticmethod
    def backward(ctx, *grad_outputs):
        xt0, = ctx.saved_tensors
        xp = ctx.xp
        output_shapes = ctx.params['output_shapes']
        dim = ctx.params['dim']
        grad0 = xp.concatenate(
            [
                xp.zeros(output_shapes[i], dtype=xt0.dtype) if gdn is None else gdn
                for i, gdn in enumerate(grad_outputs)
            ],
            axis=dim
        )
        return grad0


class Expand(Function):  # keep input _version: True
    @staticmethod
    def forward(ctx, *inputs, **params):
        xt0, = inputs
        xd0 = xt0.data
        sizes = params['sizes']
        xp = ctx.xp
        leading_dims = len(sizes) - len(xd0.shape)
        strides = [0] * leading_dims + list(xd0.strides)
        xd0_singleton_dims = []  # singleton axes to be summed during backward
        for i in range(len(sizes)):
            if i < leading_dims:  # leading dimensions
                if sizes[i] <= 0:
                    raise RuntimeError(f"The expanded size of the tensor ({sizes[i]}) isn't allowed in a leading, "
                                       f"non-existing dimension {i}")
            else:
                i -= len(sizes)  # for non-leading dimensions, count backward
                if xd0.shape[i] == 1:
                    if sizes[i] > 1:
                        xd0_singleton_dims.append(i)
                        strides[i] = 0
                else:
                    if sizes[i] != -1 and xd0.shape[i] != sizes[i]:
                        raise RuntimeError(f"The expanded size of the tensor ({sizes[i]}) must match the existing size "
                                           f"({xd0.shape[i]}) at non-singleton dimension {i + len(sizes)}.  "
                                           f"Target sizes: {sizes}.  Tensor sizes: {xd0.shape}")
        yd0 = xp.lib.stride_tricks.as_strided(xd0, shape=sizes, strides=strides)  # a numpy/cupy array
        yt0 = build_links(yd0, grad_fn=ctx)  # convert to nparray/cparray
        yt0.data._version = xd0._version  # keep version
        ctx.params = {'xd0_singleton_dims': xd0_singleton_dims, 'leading_dims': leading_dims}
        return yt0

    @staticmethod
    def backward(ctx, *grad_outputs):
        gd0, = grad_outputs
        xd0_singleton_dims = tuple(ctx.params['xd0_singleton_dims'])
        leading_dims = tuple(range(ctx.params['leading_dims']))
        grad0 = gd0.sum(xd0_singleton_dims + leading_dims, keepdims=True).squeeze(leading_dims)
        return grad0


class MaskedFill(Function):  # keep input _version: False (except in-place)
    @staticmethod
    def forward(ctx, *inputs, **params):
        xt0, xt1 = inputs
        xd0, xd1 = xt0.data, xt1.data  # input, val
        if xt1.ndim > 0:
            raise RuntimeError(f"masked_fill only supports a 0-dimensional value tensor, "
                               f"but got tensor with {xt1.ndim} dimension(s).")
        mask = params['mask']
        if mask.dtype.type is not np.bool_:
            raise RuntimeError(f"dtype of mask must be bool. "
                               f"Pass dtype=bool when constructing mask")
        flag = False
        if xd0.__class__ is cparray and xd1.__class__ is not cparray:  # xd1 is a scaler, no need to convert it to cparray
            flag = True
        elif xd0.__class__ is not cparray and xd1.__class__ is cparray:
            raise RuntimeError(f"masked_fill: Expected inputs to be on same device")

        key = (slice(None),) * (xd0.ndim - mask.ndim) + (mask.data,)
        if params['inplace']:
            inplace_precheck(xt0)
            xd0[key] = xd1
            yt0 = inplace_update(xt0, ctx)
        else:
            xd0 = xd0.copy()
            xd0[key] = xt1.data
            yt0 = build_links(xd0, grad_fn=ctx)
        ctx.params['flag'] = flag
        return yt0

    @staticmethod
    def backward(ctx, *grad_outputs):
        gd0, = grad_outputs
        mask = ctx.params['mask']
        flag = ctx.params['flag']
        leading = (slice(None),) * (gd0.ndim - mask.ndim)
        grad0, grad1 = None, None
        if ctx.needs_input_grad[1]:  # grad for value. Do this first because gd0 will be changed inplace next
            key = leading + (mask.data,)
            grad1 = gd0[key].sum()
            if flag:
                grad1 = grad1.get()
        if ctx.needs_input_grad[0]:  # grad for input
            key = leading + (mask.data,)
            grad0 = gd0
            grad0[key] = 0
        return grad0, grad1


class CopySlices(Function):  # keep input _version: True (it's inplace)
    @staticmethod
    def forward(ctx, *inputs, **params):
        xt0, xt1 = inputs
        xd0, xd1 = xt0.data, xt1.data
        key = params['key']
        # convert xd1 to same array type as xd0
        flag = None
        if xd0.__class__ is cparray and xd1.__class__ is not cparray:
            xd1 = cp.array(xd1)
            flag = True
        elif xd0.__class__ is not cparray and xd1.__class__ is cparray:
            xd1 = xd1.get()
            flag = False
        inplace_precheck(xt0)
        xd0[key] = xd1
        yt0 = inplace_update(xt0, ctx)
        ctx.params['shapes'] = (xd0.shape, xd1.shape)
        ctx.params['flag'] = flag
        return yt0

    @staticmethod
    def backward(ctx, *grad_outputs):
        gd0, = grad_outputs
        xd0_shape, xd1_shape = ctx.params['shapes']
        key = ctx.params['key']
        flag = ctx.params['flag']
        grad0, grad1 = None, None
        if ctx.needs_input_grad[1]:  # grad for value. Do this first because gd0 will be changed inplace next
            grad1 = reverse_broadcast(gd0[key], xd1_shape)
            if flag is True:
                grad1 = grad1.get()
            elif flag is False:
                grad1 = cp.array(grad1)
        if ctx.needs_input_grad[0]:  # grad for input
            grad0 = gd0
            grad0[key] = 0
        return grad0, grad1


class Copy(Function):  # keep input _version: True (it's inplace)
    @staticmethod
    def forward(ctx, *inputs, **params):
        xt0, xt1 = inputs
        xd0, xd1 = xt0.data, xt1.data

        # convert xd1 to same array type as xd0
        flag = None
        if xd0.__class__ is cparray and xd1.__class__ is not cparray:
            xd1 = cp.array(xd1)
            flag = True
        elif xd0.__class__ is not cparray and xd1.__class__ is cparray:
            xd1 = xd1.get()
            flag = False

        inplace_precheck(xt0)
        xd0[...] = xd1
        yt0 = xt0
        inplace_update(yt0, ctx)
        ctx.params['flag'] = flag
        return yt0

    @staticmethod
    def backward(ctx, *grad_outputs):
        gd0, = grad_outputs
        grad0, grad1 = None, None
        flag = ctx.params['flag']
        if ctx.needs_input_grad[1]:  # grad for value.
            grad1 = gd0
            if flag is True:
                grad1 = grad1.get()
            elif flag is False:
                grad1 = cp.array(grad1)
        if ctx.needs_input_grad[0]:  # grad for input, zero
            grad0 = gd0
            grad0[...] = 0
        return grad0, grad1  # no grad for input
