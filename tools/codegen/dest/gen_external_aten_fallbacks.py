from typing import List, Optional, Union, Dict
from typing_extensions import Literal
from dataclasses import dataclass
import re

from tools.codegen.context import method_with_native_function
from tools.codegen.utils import Target, mapMaybe
from tools.codegen.model import (Argument, ExternalBackendFunction,
                                 ExternalBackendFunctionsGroup,
                                 assert_never, Return, is_generic_dispatch_key,
                                 ListType, OptionalType, BaseType, BaseTy, Variant)
from tools.codegen.api.types import DispatcherSignature, CppSignatureGroup
import tools.codegen.api.dispatcher as dispatcher
import tools.codegen.api.cpp as cpp

# TODO: this contains a list of regex for ops that don't get a CPU fallback.
# We should just register fallthroughs when we make the CPU fallback a boxed kernel.
_FN_DENYLIST_REGEX = [
    # ATEN functions
    r'[^(]*cudnn',
    r'slow_conv_transpose2d_backward.grad_output',
    r'slow_conv_transpose3d_backward.grad_output',
    r'slow_conv3d_backward.grad_input',
    r'thnn_conv2d_backward.grad_input',
    r'thnn_conv_depthwise2d_backward.grad_input',
    # XLA/TPU functions
]

# TODO: remove this list.
# Instead, the codegen will figure out which ops to generate _out wrappers for
# entirely from the yaml. Maintaining the same behavior as current XLA codegen for now.
_FN_OUT = [
    'abs',
    'add',
    'acos',
    'acosh',
    'asin',
    'asinh',
    'atan',
    'atan2',
    'atanh',
    'baddbmm',
    'bernoulli',
    'binary_cross_entropy',
    'binary_cross_entropy_backward',
    'clamp',
    'div',
    'gather',
    'ger',
    'hardsigmoid',
    'kthvalue',
    'index_select',
    'inverse',
    'log',
    'masked_select',
    'maximum',
    'minimum',
    'pow',
    'prod',
    'nonzero',
    'round',
    'normal',
    'std',
    'take',
    'topk',
    'var',
]

def requires_backend_wrapper(f: ExternalBackendFunction) -> bool:
    requires_lowering = not any(is_generic_dispatch_key(k) for k in f.native_function.dispatch)
    has_xla_lowering = f.metadata is not None
    in_denylist = any([re.match(frx, str(f.native_function.func.name)) for frx in _FN_DENYLIST_REGEX])
    return not in_denylist and (requires_lowering or has_xla_lowering)

def xla_tensor_creation_api(
        ret_name: str,
        ret: Return,
        device_param_name: str,
        *,
        cpu_result_name: str,
        tuple_idx: Optional[int] = None
) -> str:
    if ret.type == BaseType(BaseTy.Tensor) and not ret.is_write:
        # Only raw Tensor (non-reference) returns need to go through the XLA tensor creation API.
        # Tensor references can be returned directly, since they've already been converted to XLA tensors.
        # See Note [Tensor Copy Returns]
        pass
    elif isinstance(ret.type, ListType) and ret.type.elem == BaseType(BaseTy.Tensor):
        pass
    else:
        # for non tensor-types, there's no need to wrap the output in an xla bridge api.
        return ret_name

    return f"to_device_opt({cpu_result_name}, get_device_arg({device_param_name}))"



# Generates aten_xla_type_default.h and aten_xla_type_default.cpp.
#
#   - This function registers external backend kernels, and also generates fallbacks to CPU.
#     This is useful because pretty much all external backends (e.g. XLA)
#     do not have full aten coverage.
#     For operators not implemented by the external backend, our codegen
#     will register these fallbacks instead.
#   - Why do we generate fallback for ALL (non-composite) aten ops, including ops that
#     external backends have already implemented?
#     Many external backend kernels only work with specific input shapes,
#     and are written to call into a cpu fallback when given inputs
#     that they cannot handle.
@dataclass(frozen=True)
class GenExternalAtenFallback:
    target: Union[
        Literal[Target.NAMESPACED_DEFINITION],
        Literal[Target.NAMESPACED_DECLARATION],
        Literal[Target.REGISTRATION],
    ]

    @method_with_native_function
    def __call__(self, g: Union[ExternalBackendFunctionsGroup, ExternalBackendFunction]) -> List[str]:
        def gen_unstructured_external(f: ExternalBackendFunction) -> Optional[str]:
            if not requires_backend_wrapper(f):
                return None

            def get_device_param(args: List[Argument]) -> str:
                # TODO: the XLA codegen has specific precedence rules when determining which tensor argument
                # to use as the device argument.
                # We should update this to be consistent with how we choose device guards.
                const_tensor_or_self = [
                    a for a in args if (a.type == BaseType(BaseTy.Tensor) or a.type == OptionalType(BaseType(BaseTy.Tensor)))
                    and not a.is_write]
                if any(const_tensor_or_self):
                    return const_tensor_or_self[0].name
                tensor_like = [a for a in args if a.type.is_tensor_like()]
                if any(tensor_like):
                    return tensor_like[0].name
                device_like = [a for a in args if a.type == BaseType(BaseTy.Device)
                               or a.type == OptionalType(BaseType(BaseTy.Device))]
                if any(device_like):
                    return device_like[0].name
                raise AssertionError("Need a tensor-like or device argument in order to determine the output device")

            # See Note [External Backends Follow Dispatcher convention]
            dispatcher_sig = DispatcherSignature.from_schema(f.native_function.func)
            name = dispatcher_sig.name()
            args = dispatcher_sig.arguments()

            if self.target is Target.NAMESPACED_DECLARATION:
                return f"  static {dispatcher_sig.decl()};"

            elif self.target is Target.REGISTRATION:
                # This codegen is only responsible for registering CPU fallback kernels
                # We also skip registrations if there is a functional backend kernel,
                # because we generate out/inplace wrappers in that case (handled in register_dispatch_key.py).
                if f.metadata is not None or (isinstance(g, ExternalBackendFunctionsGroup) and g.functional.metadata is not None):
                    return ''
                payload = f"static_cast<{dispatcher_sig.ptr_type()}>(&AtenXlaTypeDefault::{name})"
                return f'  m.impl("{f.native_function.func.name}", {payload});\n'

            if self.target is not Target.NAMESPACED_DEFINITION:
                assert_never(self.target)

            # Everything below here is where we generate the CPU fallback.
            # See Note [External Backends Follow Dispatcher convention]
            dispatcher_order_args = dispatcher.jit_arguments(f.native_function.func)

            # Map each argument to it's intermediate variable name in the fallback
            # We have to do it separately for TensorList/Optional<Tensor>/Tensor
            tensorlist_args: Dict[Argument, str] = {
                a: f'l_{a.name}' for a in dispatcher_order_args
                if isinstance(a.type, ListType) and a.type.elem == BaseType(BaseTy.Tensor)}

            opt_tensors = [
                a for a in dispatcher_order_args
                if isinstance(a.type, OptionalType) and a.type.elem == BaseType(BaseTy.Tensor)]
            opt_tensor_args: Dict[Argument, str] = {a: f'xlatens_opt[{i}]' for i, a in enumerate(opt_tensors)}

            tensors = [a for a in dispatcher_order_args if a.type == BaseType(BaseTy.Tensor)]
            tensor_args: Dict[Argument, str] = {a: f'xlatens[{i}]' for i, a in enumerate(tensors)}
            annotated_tensor_indices: List[int] = [
                i for i, a in enumerate(tensors) if a.annotation is not None and a.annotation.is_write]

            print_args_str = ''.join([f' << " {a.name}=" << {a.name}.toString()' for a in tensor_args.keys()])


            tensorlist_intermediates_str = ''
            if len(tensorlist_args) > 0:
                tensorlist_intermediates_str = '\n'.join([f'  auto {updated_name} = to_cpu({arg.name});'
                                                          for arg, updated_name in tensorlist_args.items()])

            opt_tensor_intermediates_str = ''
            if len(opt_tensor_args) > 0:
                arg_str = ", ".join([a.name for a in opt_tensor_args.keys()])
                opt_tensor_intermediates_str = f'\n  std::vector<c10::optional<at::Tensor>> xlatens_opt_tensors = {{{arg_str}}};'
                opt_tensor_intermediates_str += '\n  auto xlatens_opt = to_cpu(xlatens_opt_tensors);'

            intermediates = ''
            if tensorlist_intermediates_str != '':
                intermediates += tensorlist_intermediates_str + '\n'
            intermediates += f"  std::vector<at::Tensor> xlatens_tensors = {{{', '.join([a.name for a in tensor_args.keys()])}}};"
            intermediates += "\n  auto xlatens = to_cpu(xlatens_tensors);"
            if opt_tensor_intermediates_str != '':
                intermediates += opt_tensor_intermediates_str


            is_method = Variant.function not in f.native_function.variants
            func_name = f'AtenXlaTypeDefault::{name}'

            # Gather all of the updated variable names to call into the CPU operator.
            # Just use the original binding names for inputs where we didn't create explicit intermediate variables.
            updated_bindings: List[str] = [
                tensorlist_args.get(a, opt_tensor_args.get(a, tensor_args.get(a, a.name))) for a in dispatcher_order_args]

            at_call_name = CppSignatureGroup.from_native_function(
                f.native_function, method=is_method).most_faithful_signature().name()

            # Notice that we don't need to perform a translate: we're technically going from the dispatcher API
            # to the faithful C++ API, which are carefuly written to be exactly the same.
            cpu_result_name = 'x_result'
            if is_method:
                at_call = f'{updated_bindings[0]}.{at_call_name}({", ".join(name for name in updated_bindings[1:])});'
            else:
                at_call = f'at::{at_call_name}({", ".join(name for name in updated_bindings)});'
            avoid_warning = ''
            if f.native_function.func.returns:
                at_call = f'auto&& {cpu_result_name} = {at_call}'
                avoid_warning = f'\n  static_cast<void>({cpu_result_name}); // Avoid warnings in case not used'

            collect_mutated_tensors = ''
            update_tensors = ''
            if len(annotated_tensor_indices) > 0:
                indices_str = ", ".join([str(i) for i in annotated_tensor_indices])
                collect_mutated_tensors = f'\n  std::vector<size_t> xlatens_update_indices = {{{indices_str}}};'
                # TODO: uncomment the resize line below. Taken out temporarily for testing
                update_tensors = '''
  for (int i : xlatens_update_indices) {
    // if (xlatens_tensors[i].sizes() != xlatens[i].sizes()) xlatens_tensors[i].resize_(xlatens[i].sizes());
    at::_copy_from_and_resize(xlatens[i], xlatens_tensors[i]);
  }
'''

            returns = ''
            if f.native_function.func.returns:
                ret_names = cpp.return_names(f.native_function, fallback_name=cpu_result_name)
                if len(ret_names) == 1:
                    returns = xla_tensor_creation_api(
                        ret_names[0], f.native_function.func.returns[0],
                        get_device_param(dispatcher_order_args), cpu_result_name=cpu_result_name)
                else:
                    return_args = [
                        xla_tensor_creation_api(
                            ret_names[i], f.native_function.func.returns[i],
                            get_device_param(dispatcher_order_args), cpu_result_name=f'std::get<{i}>({cpu_result_name})'
                        ) for i in range(len(f.native_function.func.returns))]
                    returns = f'{dispatcher_sig.returns_type().cpp_type()}({", ".join(return_args)})'
            return_str = ''
            if returns != '':
                return_str = f'\n  return {returns};'

            return f"""\
{dispatcher_sig.defn(name=func_name)} {{
  XLA_FN_TRACK(3);
  XLA_COUNTER("aten::{name}", 1);
  TF_VLOG(3) << "XLA {name} :"{print_args_str};
{intermediates}
  {at_call}{collect_mutated_tensors}{update_tensors}{avoid_warning}{return_str}
}}

"""
        if isinstance(g, ExternalBackendFunctionsGroup):
            if g.structured:
                # We can probably only bother generating fallbacks for one of the variants, for structured
                raise AssertionError("Not Implemented")
            else:
                return list(mapMaybe(gen_unstructured_external, g.functions()))
        elif isinstance(g, ExternalBackendFunction):
            f = g
            x = gen_unstructured_external(f)
            return [x] if x else []
        else:
            assert_never(f)