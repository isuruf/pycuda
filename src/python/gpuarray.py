from __future__ import division
import numpy
from pytools import memoize
import pycuda.driver as drv



def splay(n, min_threads=None, max_threads=128, max_blocks=80):
    # stolen from cublas

    if min_threads is None:
        min_threads = WARP_SIZE

    if n < min_threads:
        block_count = 1
        elems_per_block = n
        threads_per_block = min_threads
    elif n < (max_blocks * min_threads):
        block_count = (n + min_threads - 1) // min_threads
        threads_per_block = min_threads
        elems_per_block = threads_per_block
    elif n < (max_blocks * max_threads):
        block_count = max_blocks
        grp = (n + min_threads - 1) // min_threads
        threads_per_block = ((grp + max_blocks -1) // max_blocks) * min_threads
        elems_per_block = threads_per_block
    else:
        block_count = max_blocks
        threads_per_block = max_threads
        grp = (n + min_threads - 1) // min_threads
        grp = (grp + max_blocks - 1) // max_blocks
        elems_per_block = grp * min_threads

    #print "bc:%d tpb:%d epb:%d" % (block_count, threads_per_block, elems_per_block)
    return block_count, threads_per_block, elems_per_block




NVCC_OPTIONS = []




def _get_scalar_kernel(arguments, operation, name="kernel"):
    mod = drv.SourceModule("""
        __global__ void %(name)s(%(arguments)s, int n)
        {
          int tid = threadIdx.x;
          int total_threads = gridDim.x*blockDim.x;
          int cta_start = blockDim.x*blockIdx.x;
          int i;
                
          for (i = cta_start + tid; i < n; i += total_threads) 
          {
            %(operation)s;
          }
        }
        """ % {
            "arguments": arguments, 
            "operation": operation,
            "name": name},
        options=NVCC_OPTIONS)

    return mod.get_function(name)

@memoize
def _get_axpbyz_kernel():
    return _get_scalar_kernel(
            "float a, float *x, float b, float *y, float *z",
            "z[i] = a*x[i] + b*y[i]",
            "axpbyz")

@memoize
def _get_axpbz_kernel():
    return _get_scalar_kernel(
            "float a, float *x,float b, float *z",
            "z[i] = a * x[i] + b",
            "axpb")

@memoize
def _get_multiply_kernel():
    return _get_scalar_kernel(
            "float *x, float *y, float *z",
            "z[i] = x[i] * y[i]",
            "multiply")

@memoize
def _get_divide_kernel():
    return _get_scalar_kernel(
            "float *x, float *y, float *z",
            "z[i] = x[i] / y[i]",
            "divide")

@memoize
def _get_rdivide_scalar_kernel():
    return _get_scalar_kernel(
            "float *x, float y, float *z",
            "z[i] = y / x[i]",
            "divide_r")




@memoize
def _get_fill_kernel():
    return _get_scalar_kernel(
            "float a, float *z",
            "z[i] = a",
            "fill")




WARP_SIZE = 32



class GPUArray(object): 
    """A GPUArray is used to do array based calculation on the GPU. 

    This is mostly supposed to be a numpy-workalike. Operators
    work on an element-by-element basis, just like numpy.ndarray.
    """

    def __init__(self, shape, dtype, stream=None):
        self.shape = shape
        self.dtype = numpy.dtype(dtype)
        from pytools import product
        self.size = product(shape)
        if self.size:
            self.gpudata = drv.mem_alloc(self.size * self.dtype.itemsize)
        else:
            self.gpudata = None
        self.stream = stream

        self._update_kernel_kwargs()

    def _update_kernel_kwargs(self):
        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        self._kernel_kwargs = {
                "block": (threads_per_block,1,1), 
                "grid": (block_count,1),
                "stream": self.stream,
        }

    @classmethod
    def compile_kernels(cls):
        # useful for benchmarking
        for name in dir(cls.__module__):
            if name.startswith("_get_") and name.endswith("_kernel"):
                name()

    def set(self, ary, stream=None):
        assert ary.size == self.size
        assert ary.dtype == self.dtype
        if self.size:
            drv.memcpy_htod(self.gpudata, ary, stream)

    def get(self, ary=None, stream=None, pagelocked=False):
        if ary is None:
            if pagelocked:
                ary = drv.pagelocked_empty(self.shape, self.dtype)
            else:
                ary = numpy.empty(self.shape, self.dtype)
        else:
            assert ary.size == self.size
            assert ary.dtype == self.dtype
        if self.size:
            drv.memcpy_dtoh(ary, self.gpudata)
        return ary

    def __str__(self):
        return str(self.get())

    def __repr__(self):
        return repr(self.get())




    # kernel invocation wrappers ----------------------------------------------
    def _axpbyz(self, selffac, other, otherfac, out):
        """Compute ``out = selffac * self + otherfac*other``, 
        where `other` is a vector.."""
        assert self.dtype == numpy.float32
        assert self.shape == other.shape
        assert self.dtype == other.dtype

        if self.stream is not None or other.stream is not None:
            assert self.stream is other.stream

        _get_axpbyz_kernel()(numpy.float32(selffac), self.gpudata, 
                numpy.float32(otherfac), other.gpudata, 
                out.gpudata, numpy.int32(self.size),
                **self._kernel_kwargs)

        return out

    def _axpbz(self, selffac, other, out):
        """Compute ``out = selffac * self + other``, where `other` is a scalar."""
        assert self.dtype == numpy.float32

        _get_axpbz_kernel()(
                numpy.float32(selffac), self.gpudata,
                numpy.float32(other),
                out.gpudata, numpy.int32(self.size),
                **self._kernel_kwargs)

        return out

    def _elwise_multiply(self, other, out):
        assert self.dtype == numpy.float32
        assert self.dtype == numpy.float32

        _get_multiply_kernel()(
                self.gpudata, other.gpudata,
                out.gpudata, numpy.int32(self.size),
                **self._kernel_kwargs)

        return out

    def _rdiv_scalar(self, other, out):
        """Divides an array by a scalar::
          
           y = n / self 
        """

        assert self.dtype == numpy.float32

        _get_rdivide_scalar_kernel()(
                self.gpudata,
                numpy.float32(other),
                out.gpudata, numpy.int32(self.size),
                **self._kernel_kwargs)

        return out

    def _div(self, other, out):
        """Divides an array by another array."""

        assert self.dtype == numpy.float32
        assert self.shape == other.shape
        assert self.dtype == other.dtype

        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        _get_divide_kernel()(self.gpudata, other.gpudata,
                out.gpudata, numpy.int32(self.size),
                **self._kernel_kwargs)

        return out



    # operators ---------------------------------------------------------------
    def __add__(self, other):
        """Add an array with an array or an array with a scalar."""

        if isinstance(other, (int, float, complex)):
            # add a scalar
            if other == 0:
                return self
            else:
                result = GPUArray(self.shape, self.dtype)
                return self._axpbz(1, other, result)
        else:
            # add another vector
            result = GPUArray(self.shape, self.dtype)
            return self._axpbyz(1, other, 1, result)

    __radd__ = __add__

    def __sub__(self, other):
        """Substract an array from an array or a scalar from an array."""

        if isinstance(other, (int, float, complex)):
            # if array - 0 than just return the array since its the same anyway

            if other == 0:
                return self
            else:
                # create a new array for the result
                result = GPUArray(self.shape, self.dtype)
                return self._axpbz(1, -other, result)
        else:
            result = GPUArray(self.shape, self.dtype)
            return self._axpbyz(1, other, -1, result)

    def __rsub__(self,other):
        """Substracts an array by a scalar or an array:: 

           x = n - self
        """
        assert isinstance(other, (int, float, complex))

        # if array - 0 than just return the array since its the same anyway
        if other == 0:
            return self
        else:
            # create a new array for the result
            result = GPUArray(self.shape, self.dtype)
            return self._axpbz(-1, other, result)

    def __iadd__(self, other):
        return self._axpbyz(1, other, 1, self)

    def __isub__(self, other):
        return self._axpbyz(1, other, -1, self)

    def __neg__(self):
        result = GPUArray(self.shape, self.dtype)
        return self._axpbz(-1, 0, result)

    def __mul__(self, other):
        result = GPUArray(self.shape, self.dtype)
        if isinstance(other, (int, float, complex)):
            return self._axpbz(other, 0, result)
        else:
            return self._elwise_multiply(other, result)

    def __rmul__(self, scalar):
        result = GPUArray(self.shape, self.dtype)
        return self._axpbz(scalar, 0, result)

    def __imul__(self, scalar):
        return self._axpbz(scalar, 0, self)

    def __div__(self, other):
        """Divides an array by an array or a scalar::

           x = self / n
        """
        if isinstance(other, (int, float, complex)):
            # if array - 0 than just return the array since its the same anyway
            if other == 0:
                return self
            else:
                # create a new array for the result
                result = GPUArray(self.shape, self.dtype)
                return self._axpbz(1/other, 0, result)
        else:
            result = GPUArray(self.shape, self.dtype)
            return self._div(other, result)

    def __rdiv__(self,other):
        """Divides an array by a scalar or an array::

           x = n / self
        """

        if isinstance(other, (int, float, complex)):
            # if array - 0 than just return the array since its the same anyway
            if other == 0:
                return self
            else:
                # create a new array for the result
                result = GPUArray(self.shape, self.dtype)
                return self._rdiv_scalar(other, result)
        else:
            result = GPUArray(self.shape, self.dtype)

            assert self.dtype == numpy.float32
            assert self.shape == other.shape
            assert self.dtype == other.dtype

            _get_divide_kernel()(other.gpudata, self.gpudata,
                    out.gpudata, numpy.int32(self.size),
                    **self._kernel_kwargs)

            return result


    def fill(self, value):
        assert self.dtype == numpy.float32

        block_count, threads_per_block, elems_per_block = splay(self.size, WARP_SIZE, 128, 80)

        _get_fill_kernel()(numpy.float32(value), self.gpudata, numpy.int32(self.size),
                block=(threads_per_block,1,1), grid=(block_count,1),
                stream=self.stream)

        return self

    def bind_to_texref(self, texref):
        texref.set_address(self.gpudata, self.size*self.dtype.itemsize)





def to_gpu(ary, stream=None):
    result = GPUArray(ary.shape, ary.dtype)
    result.set(ary, stream)
    return result




empty = GPUArray

def zeros(shape, dtype, stream=None):
    result = GPUArray(shape, dtype, stream)
    result.fill(0)
    return result
