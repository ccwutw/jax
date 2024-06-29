# ---
# Copyright 2024 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import TypeAlias, Union, Sequence, Optional, Any, Callable, TypeVar
from contextlib import contextmanager
from dataclasses import dataclass

from util import *

# === pre-declared aliases to avoid toposorting issues ===

LazyJaxpr : TypeAlias = Any
Jaxpr     : TypeAlias = Any
JaxprType : TypeAlias = Any

# === data ===

class JaxType:
  def __eq__(self, other): raise NotImplementedError(type(self))
  def __str__(self): raise NotImplementedError(type(self))

class JaxVal:
  @property
  def ty(self):       raise NotImplementedError(type(self))
  def __str__(self):  raise NotImplementedError(type(self))

@dataclass
class Var:
  ty : JaxType
  def __str__(self):
    s = id(self)%1000 # hack!
    return f'v{s}:{str(self.ty)}'
  def __hash__(self): return id(self)
  def __eq__(self, other): return self is other

  # these methods defer to the type
  def __getitem__(self, ix):
    return self.ty._getitem(self, ix)

Atom : TypeAlias = Var | JaxVal

# === primitives ops ===

class Op:
  @property
  def ir_level(self):          raise NotImplementedError(type(self))
  def __str__(self):           raise NotImplementedError(type(self))

  def result_type(self, *args):  raise NotImplementedError(type(self))

  # MdJax ops only
  def jvp(self, primals:list[Atom], tangents:list[Atom]): raise NotImplementedError(type(self))
  # LoJax ops only
  def impl(self, *args):       raise NotImplementedError(type(self))

class Hof:
  @property
  def ir_level(self):          raise NotImplementedError(type(self))
  def __str__(self):           raise NotImplementedError(type(self))


  # MdJax ops only
  def jvp(self, funargs, primals, tangents): raise NotImplementedError(type(self))
  # LoJax ops only
  def impl(self, *args_and_funargs): raise NotImplementedError(type(self))

Primitive : TypeAlias = Op | Hof

# === function-valued arguments ===

FunArg : TypeAlias = Any

@dataclass
class JaxprEqn:
  binder: Var
  op: Op
  args: list[Atom]
  funargs: list[Jaxpr]
  def __str__(self): return PrettyPrinter.to_str(self)
  def pretty_print(self, p):
    p.print_line(f'{self.binder} = {self.op}{arglist_str(self.args)}')
    with p.indent():
      for jaxpr in self.funargs:
        jaxpr.pretty_print(p)

@dataclass
class Jaxpr:
  binders: list[Var]
  eqns: list[JaxprEqn]
  result: Atom

  @property
  def ty(self) -> JaxprType:
    return JaxprType([b.ty for b in self.binders], self.result.ty)

  def materialize(self) -> Jaxpr: return self
  def to_lazy_jaxpr(self) -> LazyJaxpr:
    assert False
  def __str__(self): return PrettyPrinter.to_str(self)
  def pretty_print(self, p):
    p.print_line(f'{arglist_str(self.binders)} =>')
    with p.indent():
      for eqn in self.eqns:
        eqn.pretty_print(p)
      p.print_line(f'return {self.result}')

@dataclass
class JaxprType:
  arg_types:  list[JaxType]
  result_type : JaxType
  def __str__(self): return f'{arglist_str(self.arg_types)} -> {self.result_type}'

# An `OpStream` is a function that takes an Emitter object explicitly
OpStream : TypeAlias = Callable # [[Emitter, ...], ...]

@dataclass
class LazyJaxpr:
  arg_types : list[JaxType]
  run : OpStream  # Callable[[Emitter, arg1, arg2, ..]], result]

  def materialize(self) -> Jaxpr:
    binders = [new_var(t) for t in self.arg_types]
    emitter = BuilderEmitter()
    result = canonicalize_pyval(self.run(emitter, *binders))
    return Jaxpr(binders, emitter.eqns, result)

  def to_lazy_jaxpr(self) -> Jaxpr:
    return self

FunArg : TypeAlias = Jaxpr | LazyJaxpr

# === program transformations ===

class Emitter:
  def emit(self, op:Op, args, funargs):
    raise NotImplementedError(type(self))

# === Evaluation with concrete values ===

class EvalEmitter(Emitter):
  def emit(self, p:Primitive, args:tuple[Atom], funargs:tuple[FunArg]):
    funargs_lazy = [f.to_lazy_jaxpr() for f in funargs]
    return p.impl(*(tuple(args) + tuple(funargs_lazy)))

# === Builder ===

def new_var(ty):
  # keeping some indirection to make room for an explicit counter or something
  return Var(ty)

class BuilderEmitter(Emitter):
  def __init__(self):
    self.eqns = []

  def emit(self, p:Primitive, args, funargs):
    arg_tys = tuple(arg.ty for arg in args)
    jaxprs = tuple(f.materialize() for f in funargs)
    funarg_tys = tuple(j.ty for j in jaxprs)
    result_ty = p.result_type(*(arg_tys + funarg_tys))
    v = new_var(result_ty)
    self.eqns.append(JaxprEqn(v, p, args, jaxprs))
    return v

# === embedding ===

# We keep a "current emitter" as a globally-readable context. This is purely to
# reduce clutter in user-facing code. Internally we pass around emitters
# explicitly so it's easier to follow the flow of data.
@dataclass
class CurrentEmitter:
  emitter : Emitter

@contextmanager
def set_current_emitter(emitter):
  prev = current_emitter.emitter
  current_emitter.emitter = emitter
  try:
    yield
  finally:
    current_emitter.emitter = prev

eval_emitter = EvalEmitter()
current_emitter = CurrentEmitter(eval_emitter)

def emit(p, args, funargs=()):
  return current_emitter.emitter.emit(p, args, funargs)

# The callable should take the emitter *implicitly*
def py_function_as_lazy_jaxpr(f:Callable, arg_types) -> LazyJaxpr:
  return LazyJaxpr(arg_types, WithExplicitEmitter(f))

# This turns a function that reads the implicit "current_emitter" context into
# one that takes the emitter explicitly, conforming to the `OpStream` API
@dataclass
class WithExplicitEmitter:
  f : Callable
  def __call__(self, emitter, *args):
    with set_current_emitter(emitter):
      return self.f(*args)

# === loose-to-strict conversion ===

PyVal : TypeAlias = Any
pyval_canonicalizers = {}

def register_canonicalizer(t, f):
  pyval_canonicalizers[t] = f

def canonicalize_pyval(x: PyVal) -> Atom:
  if isinstance(x, JaxVal):
    return x
  elif isinstance(x, Var):
    return x
  elif type(x) in pyval_canonicalizers:
    return pyval_canonicalizers[type(x)](x)
  else:
    raise TypeError(f'Unrecognized type: {type(x)}')

# === trace-to-jaxpr utility. For debugging and staging out of python only ===

def trace_to_jaxpr(f, arg_types:list[JaxType]) -> Jaxpr:
  return py_function_as_lazy_jaxpr(f, arg_types).materialize()
