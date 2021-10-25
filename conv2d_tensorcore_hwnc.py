import numpy as np
import tvm
from tvm import te
from tvm import autotvm
from ..util import get_const_tuple, traverse_inline, simplify, tag
from ..nn.pad import pad
from ..nn.util import get_pad_tuple
from topi.cuda.injective import schedule_injective_from_existing
from .tensor_intrin import intrin_wmma_load_matrix_A, intrin_wmma_load_matrix_W, intrin_wmma_store_matrix, intrin_wmma_gemm

def unpack_HWNCnc_to_hwnc(packed_out, out_dtype):
    """Unpack conv2d_hwnc output from layout hwncnc to hwnc

     Parameters
    -----------
    packed_out : tvm.te.Tensor
        The output tensor of conv2d_hwnc.

    out_dtype : str
        The output dtype.

    Returns
    -------
    unpacked_out : tvm.te.Tensor
        The unpacked output tensor in hwnc layout.
    """
    H, W, N, O, wmma_m, wmma_n = get_const_tuple(packed_out.shape)

    idxmod = tvm.tir.indexmod
    idxdiv = tvm.tir.indexdiv

    oshape = (H, W, N * wmma_m, O * wmma_n)
    unpacked_out = \
        te.compute(oshape,
                   lambda h, w, n, o:
                   packed_out[h, w, idxdiv(n, wmma_m), idxdiv(o, wmma_n), idxmod(n, wmma_m), idxmod(o, wmma_n)]
                   .astype(out_dtype),
                   name='output_unpack',
                   tag=tag.INJECTIVE+",unpack_hwncc")
    return unpacked_out

def conv2d_hwnc_tensorcore(data, kernel, strides, padding, dilation, in_dtype, out_dtype='int32'):
    """Compute conv2d internally using conv2d_nchwc layout for int8 dtype"""
    assert data.dtype in ('int4', 'uint4', 'int8', 'uint8')
    assert kernel.dtype in ('int4', 'uint4', 'int8', 'uint8')
    # assert data.dtype == kernel.dtype
    packed_out = hwnc_tensorcore_cuda(data, kernel, strides, padding, dilation, out_dtype)
    return unpack_HWNCnc_to_hwnc(packed_out, out_dtype)

# def schedule_conv2d_hwnc_tensorcore(outs):
#     """Create schedule for tensors"""
#     return schedule_conv2d_hwnc_tensorcore(outs)

@autotvm.register_topi_compute("conv2d_HWNCnc_tensorcore.cuda")
def hwnc_tensorcore_cuda(cfg, Input, Filter, stride, padding, dilation, out_dtype='int32'):
    """Compute declaration for tensorcore"""
    assert isinstance(stride, int) or len(stride) == 2
    assert isinstance(dilation, int) or len(dilation) == 2

    def upalign(x, align):
        return (x + align - 1) // align * align

    if isinstance(stride, int):
        stride_h = stride_w = stride
    else:
        stride_h, stride_w = stride

    if isinstance(dilation, int):
        dilation_h = dilation_w = dilation
    else:
        dilation_h, dilation_w = dilation

    in_dtype = Input.dtype

    if in_dtype in ['int4', 'uint4']:
        wmma_n = wmma_m = 8
        wmma_k = 32
    else:
        wmma_m = 8
        wmma_n = 32
        wmma_k = 16

    pre_computed = len(Filter.shape) == 6
    in_height, in_width, batch, in_channels = get_const_tuple(Input.shape)
    if pre_computed:
        kernel_h, kernel_w, oc_chunk, ic_chunk, oc_block_factor, ic_block_factor = get_const_tuple(Filter.shape)
        num_filter = oc_block_factor * oc_chunk
    else:
        kernel_h, kernel_w, num_filter, _ = get_const_tuple(Filter.shape)

    if in_dtype in ['int4', 'uint4']:
        assert (batch % 8 == 0 and in_channels % 32 == 0 and num_filter % 8 == 0)
    else:
        assert (batch % 16 == 0 and in_channels % 16 == 0 and num_filter % 16 == 0) or \
               (batch % 8 == 0 and in_channels % 16 == 0 and num_filter % 32 == 0) or \
               (batch % 32 == 0 and in_channels % 16 == 0 and num_filter % 8 == 0), \
               "The shape of (batch, in_channels, num_filter) "\
               "must be multiple of (16, 16, 16) or (32, 16, 8) or (8, 16, 32) for fp16 and int8, "\
               "and (8, 32, 8) for int4"

    # compute the output shape
    dilated_kernel_h = (kernel_h - 1) * dilation_h + 1
    dilated_kernel_w = (kernel_w - 1) * dilation_w + 1

    pad_top, pad_left, pad_down, pad_right = get_pad_tuple(
        padding, (dilated_kernel_h, dilated_kernel_w))

    out_channels = num_filter
    out_height = simplify((in_height - dilated_kernel_h + pad_top + pad_down) // stride_h + 1)
    out_width = simplify((in_width - dilated_kernel_w + pad_left + pad_right) // stride_w + 1)

    # cfg.add_flop(2 * batch * out_height * out_width * out_channels * in_channels * kernel_h * kernel_w)

    # Input feature map: (N, H, W, IC, n, ic)
    data_shape = (in_height,
                  in_width,
                  batch // wmma_m,
                  in_channels // wmma_k,
                  wmma_m,
                  wmma_k)

    # Kernel: (H, W, OC, IC, ic, oc)
    kernel_shape = (kernel_h,
                    kernel_w,
                    out_channels // wmma_n,
                    in_channels // wmma_k,
                    wmma_n,
                    wmma_k)

    # Reduction axes
    kh = te.reduce_axis((0, kernel_h), name='kh')
    kw = te.reduce_axis((0, kernel_w), name='kw')
    ic = te.reduce_axis((0, in_channels // wmma_k), name='ic')
    ii = te.reduce_axis((0, wmma_k), name='ii')

    if pre_computed:
        packed_kernel = Filter
    else:
        packed_kernel = te.compute(kernel_shape,
            lambda kh, kw, o, i, oo, ii: Filter[kh, kw, o * wmma_n + oo, i * wmma_k + ii],
            name="packed_kernel"
        )

    pad_before = [pad_top, pad_left, 0, 0]
    pad_after = [pad_down, pad_right, 0, 0]
    pad_data = pad(Input, pad_before, pad_after, name="pad_data")

    packed_data = te.compute(data_shape,
        lambda h, w, n, i, nn, ii: pad_data[h, w, n * wmma_m + nn, i * wmma_k + ii]
    )

    Conv = te.compute((out_height, out_width,  batch // wmma_m, out_channels // wmma_n,wmma_m, wmma_n),
                    lambda h, w, n, o, nn, oo: te.sum(
                        (packed_data[h * stride_h + kh, w * stride_w + kw, n, ic, nn, ii].astype('int32') *
                        packed_kernel[kh, kw, o, ic, oo, ii].astype('int32')),
                        axis=[ic, kh, kw, ii]),
                    name="Conv", tag="conv2d_HWNCnc_tensorcore")
    return Conv


def schedule_hwnc_tensorcore_cuda(cfg, s, Conv):
    packed_data, packed_kernel = s[Conv].op.input_tensors
    ic, kh, kw, ii = s[Conv].op.reduce_axis
    pad_data = s[packed_data].op.input_tensors[0]

    block_x = te.thread_axis('blockIdx.x')
    block_y = te.thread_axis('blockIdx.y')
    block_z = te.thread_axis('blockIdx.z')
    thread_x = te.thread_axis('threadIdx.x')
    thread_y = te.thread_axis('threadIdx.y')
    thread_z = te.thread_axis('threadIdx.z')

    # Designate the memory hierarchy
    AS = s.cache_read(packed_data, 'shared', [Conv])
    WS = s.cache_read(packed_kernel, 'shared', [Conv])
    AF = s.cache_read(AS, 'wmma.matrix_a', [Conv])
    WF = s.cache_read(WS, 'wmma.matrix_b', [Conv])
    ConvF = s.cache_write(Conv, 'wmma.accumulator')

    if Conv.op in s.outputs:
        output = Conv
        ConvS = s.cache_read(ConvF, 'shared', [Conv])
        OL = ConvS
    else:
        output = s.outputs[0].output(0)
        s[Conv].set_scope('shared')
        OL = Conv

    out_dtype = Conv.dtype

    if isinstance(packed_kernel.op, te.tensor.ComputeOp) and packed_kernel.name == "packed_kernel":
        if autotvm.GLOBAL_SCOPE.in_tuning:
            s[packed_kernel].pragma(s[packed_kernel].op.axis[0], "debug_skip_region")
        else:
                # with tvm.target.create('cuda'):
            schedule_injective_from_existing(s, packed_kernel)

    if isinstance(pad_data.op, te.tensor.ComputeOp) and "pad" in pad_data.op.tag:
        s[pad_data].compute_inline()
        data = pad_data.op.input_tensors[0]

        if autotvm.GLOBAL_SCOPE.in_tuning:
            # skip this part during tuning to make recrods accurate
            # this part will be pre-computed during NNVM's pre-compute optimization pass
            s[pad_data].pragma(s[pad_data].op.axis[0], "debug_skip_region")
    else:
        data = pad_data

    data_dtype = data.dtype
    kernel_dtype = packed_kernel.dtype

    # Schedule for autotvm
    cfg.define_knob("block_row_warps", [1, 2, 4])
    cfg.define_knob("block_col_warps", [1, 2, 4])
    cfg.define_knob("warp_row_tiles", [1, 2, 4, 8, 16])
    cfg.define_knob("warp_col_tiles", [1, 2, 4, 8, 16])
    cfg.define_knob("chunk", [1, 2, 4, 8])
    cfg.define_knob("fuse_pack", [0, 1])
    cfg.define_knob("split_block_k", [1, 2, 4, 8, 16, 32])
    cfg.define_knob("vector_ws", [1, 8])
    cfg.define_knob("vector_as", [1, 8, 16])
    block_row_warps = cfg["block_row_warps"].val
    block_col_warps = cfg["block_col_warps"].val
    warp_row_tiles = cfg["warp_row_tiles"].val
    warp_col_tiles = cfg["warp_col_tiles"].val
    chunk = cfg["chunk"].val
    vector_as = cfg["vector_as"].val
    vector_ws = cfg["vector_ws"].val
    split_block_k = cfg["split_block_k"].val
    fuse_pack = cfg["fuse_pack"].val
    # block_row_warps = 1
    # block_col_warps = 2
    # warp_row_tiles = 8
    # warp_col_tiles = 4
    # chunk = 4
    # vector_ws = 1
    # fuse_pack = 1
    # vector_as = 8
    # split_block_k = 1

    if not fuse_pack:
        s[packed_data].compute_inline()
    else:
        with tvm.target.create('cuda'):
            schedule_injective_from_existing(s, packed_data)

    if data_dtype in ['int4', 'uint4']:
        wmma_m = wmma_n = 8
        wmma_k = 32
    else:
        wmma_m = 8
        wmma_n = 32
        wmma_k = 16

    warp_size = 32

    # Schedule for output
    if len(s[output].op.axis) == 4:
        hc, wc, nc, oc,  = output.op.axis
        nc, nnc = s[output].split(nc, factor=wmma_m)
        oc, ooc = s[output].split(oc, factor=wmma_n)
    else:
        hc, wc, nc, oc, nnc, ooc = output.op.axis

    kernel_scope, hc = s[output].split(hc, nparts=1)

    # block_k = s[output].fuse(hc, wc)

    block_i = s[output].fuse(nc, hc, wc) # nc, hc, wc = 1, 7, 7

    # block_k, sub_block_k = s[output].split(block_k, factor=split_block_k)

    block_i, nci = s[output].split(block_i, factor=warp_row_tiles)
    block_i, nc = s[output].split(block_i, factor=block_row_warps)
    oc, oci = s[output].split(oc, factor=warp_col_tiles)
    block_j, oc = s[output].split(oc, factor=block_col_warps)
    # s[output].reorder(block_k, sub_block_k, block_i, block_j, nc, oc, nci, oci, nnc, ooc)
    s[output].reorder(block_i, block_j, nc, oc, nci, oci, nnc, ooc)
    t = s[output].fuse(nnc, ooc)
    _, tx = s[output].split(t, factor=warp_size)

    # s[output].bind(block_k, block_z)
    
    s[output].bind(block_i, block_x)
    s[output].bind(block_j, block_y)
    s[output].bind(tx, thread_x)
    s[output].bind(nc, thread_y)
    s[output].bind(oc, thread_z)

    # Schedule wmma store
    s[OL].compute_at(s[output], block_j)
    hc, wc, nc, oc, nnc, ooc = OL.op.axis
    oc, oci = s[OL].split(oc, factor=warp_col_tiles)
    _, oc = s[OL].split(oc, factor=block_col_warps)
    nc, nci = s[OL].split(nc, factor=warp_row_tiles)
    _, nc = s[OL].split(nc, factor=block_row_warps)
    s[OL].reorder(nc, oc, nci, oci, nnc, ooc)
    s[OL].bind(nc, thread_y)
    s[OL].bind(oc, thread_z)

    # Schedule local computation
    s[ConvF].compute_at(s[OL], oc)
    h, w, n, o, nnf, oof = ConvF.op.axis
    ko, ki = s[ConvF].split(ic, factor=chunk)
    s[ConvF].reorder(ko, kh, ki, kw, n, o, nnf, oof, ii)

    cfg.define_reorder("reorder_inner", [ko, kh], policy="all")
    cfg["reorder_inner"].apply(s, ConvF, [ko, kh])
    cfg["reorder_inner"].apply(s, ConvF, [ki, kw])

    cfg.define_knob("compute_at_AS", [0, 1, 2, 3])
    cfg.define_knob("compute_at_WS", [0, 1, 2, 3])
    compute_at_AS = cfg["compute_at_AS"].val
    compute_at_WS = cfg["compute_at_WS"].val

    # Move intermediate computation into each output compute tile

    s[AF].compute_at(s[ConvF], kw)
    s[WF].compute_at(s[ConvF], kw)

    # Schedule for A's share memory
    if compute_at_AS == 0:
        s[AS].compute_at(s[ConvF], ki)
    elif compute_at_AS == 1:
        s[AS].compute_at(s[ConvF], kw)
    elif compute_at_AS == 2:
        s[AS].compute_at(s[ConvF], ko)
    else:
        s[AS].compute_at(s[ConvF], kh)
    # s[AS].compute_at(s[ConvF], kh)
    h, w, n, i, nn, ii = AS.op.axis
    tx, xo = s[AS].split(n, nparts=block_row_warps)
    ty, yo = s[AS].split(xo, nparts=block_col_warps)
    t = s[AS].fuse(nn, ii)
    to, ti = s[AS].split(t, nparts=warp_size)
    ti, _t = s[AS].split(ti, factor=vector_as)
    s[AS].bind(tx, thread_y)
    s[AS].bind(ty, thread_z)
    s[AS].bind(to, thread_x)
    s[AS].vectorize(_t)

    # Schedule for W's share memory
    if compute_at_WS == 0:
        s[WS].compute_at(s[ConvF], ki)
    elif compute_at_WS == 1:
        s[WS].compute_at(s[ConvF], kw)
    elif compute_at_WS == 2:
        s[WS].compute_at(s[ConvF], ko)
    else:
        s[WS].compute_at(s[ConvF], kh)
    s[WS].compute_at(s[ConvF], kw)
    kh, kw, ic, o, ii, oo = WS.op.axis
    tx, xo = s[WS].split(o, nparts=block_row_warps)
    ty, yo = s[WS].split(xo, nparts=block_col_warps)
    t = s[WS].fuse(ii, oo)
    to, ti = s[WS].split(t, nparts=warp_size)
    ti, _t = s[WS].split(ti, factor=vector_ws)
    s[WS].bind(tx, thread_y)
    s[WS].bind(ty, thread_z)
    s[WS].bind(to, thread_x)
    s[WS].vectorize(ti)

    # double buffer
    cfg.define_knob('AS_double_buffer', [0, 1])
    cfg.define_knob('WS_double_buffer', [0, 1])
    if cfg['AS_double_buffer'].val:
        s[AS].double_buffer()
    if cfg['WS_double_buffer'].val:
        s[WS].double_buffer()

    # unroll
    cfg.define_knob("auto_unroll_max_step", [0, 512, 1500])
    s[output].pragma(kernel_scope, 'auto_unroll_max_step',
                     cfg['auto_unroll_max_step'].val)
    s[output].pragma(kernel_scope, 'unroll_explicit', False)



    shape = (wmma_m, wmma_n, wmma_k)

    AS_shape = (wmma_m, wmma_k)
    AL_shape = (wmma_m, wmma_k)
    WS_shape = (wmma_n, wmma_k)
    WL_shape = (wmma_n, wmma_k)
    CL_shape = (wmma_m, wmma_n)
    CS_shape = (wmma_m, wmma_n)

    AL_gemm = te.placeholder(AL_shape, name='A', dtype=data_dtype)
    WL_gemm = te.placeholder(WL_shape, name='B', dtype=kernel_dtype)
    k_gemm = te.reduce_axis((0, wmma_k), name="k")
    CL_compute = te.compute(CL_shape, lambda ii, jj:
                    te.sum((AL_gemm[ii, k_gemm].astype('int32')* WL_gemm[jj, k_gemm].astype('int32')), axis=k_gemm),
                    name='C')

    AL_strides = [wmma_k, 1]
    AS_strides = [wmma_k, 1]
    WL_strides = [wmma_k, 1]
    WS_strides = [wmma_k, 1]
    CL_strides = [wmma_n, 1]
    CS_strides = [wmma_n, 1]

    s[AF].tensorize(AF.op.axis[-2], intrin_wmma_load_matrix_A(AL_strides, AS_strides, shape,
                                                    "row_major", AS_shape, AL_shape, data_dtype))

    s[WF].tensorize(WF.op.axis[-2], intrin_wmma_load_matrix_W(WL_strides, WS_strides, shape,
                                                    "col_major", WS_shape, WL_shape, kernel_dtype))

    s[OL].tensorize(nnc, intrin_wmma_store_matrix(CS_strides, CL_strides,
                                                  shape, out_dtype, CL_shape, CS_shape, C_scope='shared'))


    s[ConvF].tensorize(nnf, intrin_wmma_gemm(AL_gemm, WL_gemm, CL_compute, AL_strides,
                                             WL_strides, CL_strides, shape))

    # print(tvm.lower(s, [data, packed_kernel, Conv], simple_mode=True))


    return s

# @autotvm.register_topi_compute("conv2d_HWNCnc_tensorcore.cuda")
# def conv2d_hwnc_tensorcore(cfg, data, kernel, strides, padding, dilation, in_dtype, out_dtype='int32'):
#     """Compute conv2d with tensorcore for NCHW layout"""
#     return hwnc_tensorcore_cuda(cfg, data, kernel, strides, padding, dilation, out_dtype)


@autotvm.register_topi_schedule("conv2d_HWNCnc_tensorcore.cuda")
def schedule_conv2d_hwnc_tensorcore(cfg, outs):
    """TOPI schedule callback"""
    s = te.create_schedule([x.op for x in outs])
    def _callback(op):
        if 'conv2d_HWNCnc_tensorcore' in op.tag:
            schedule_hwnc_tensorcore_cuda(cfg, s, op.output(0))

    traverse_inline(s, outs[0].op, _callback)
    return s

