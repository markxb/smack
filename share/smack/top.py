import argparse
import errno
import io
import json
import os
import platform
import re
import shutil
import sys
import shlex
import subprocess
from svcomp.utils import svcomp_frontend
from svcomp.utils import verify_bpl_svcomp
from utils import temporary_file, try_command, remove_temp_files
from replay import replay_error_trace

VERSION = '1.9.0'

def languages():
  """A dictionary of languages per file extension."""
  return {
    'c'      : 'C',
    'i'      : 'C',
    'cc'     : 'CXX',
    'cpp'    : 'CXX',
    'm'      : 'OBJC',
    'd'      : 'D',
    'json'   : 'JSON',
    'svcomp' : 'SVCOMP',
    'bc'     : 'LLVM',
    'll'     : 'LLVM',
    'bpl'    : 'BOOGIE',
    'f'      : 'FORTRAN',
    'for'    : 'FORTRAN',
    'f90'    : 'FORTRAN',
    'f95'    : 'FORTRAN',
    'f03'    : 'FORTRAN',
  }

def frontends():
  """A dictionary of front-ends per language."""
  return {
    'C'        : clang_frontend,
    'CXX'      : clang_plusplus_frontend,
    'OBJC'     : clang_objc_frontend,
    'D'        : d_frontend,
    'JSON'     : json_compilation_database_frontend,
    'SVCOMP'   : svcomp_frontend,
    'LLVM'     : llvm_frontend,
    'BOOGIE'   : boogie_frontend,
    'FORTRAN'  : fortran_frontend,
  }

def extra_libs():
  """A dictionary of extra SMACK libraries required by languages."""
  return {
    'FORTRAN' : fortran_build_libs, 
    'CXX'     : cplusplus_build_libs,
    # coming soon - libraries for OBJC, Rust, Swift, etc.
  }

def results(args):
  """A dictionary of the result output messages."""
  return {
    'verified': 'SMACK found no errors.' if args.modular else 'SMACK found no errors with unroll bound %s.' % args.unroll,
    'error': 'SMACK found an error.',
    'invalid-deref': 'SMACK found an error: invalid pointer dereference.',
    'invalid-free': 'SMACK found an error: invalid memory deallocation.',
    'invalid-memtrack': 'SMACK found an error: memory leak.',
    'overflow': 'SMACK found an error: integer overflow.',
    'timeout': 'SMACK timed out.',
    'unknown': 'SMACK result is unknown.'
  }

def inlined_procedures():
  return [
    '$galloc',
    '$alloc',
    '$malloc',
    '$free',
    '$memset',
    '$memcpy',
    '__VERIFIER_',
    '$initialize',
    '__SMACK_static_init',
    '__SMACK_init_func_memory_model',
    '__SMACK_check_overflow'
  ]

def validate_input_file(file):
  """Check whether the given input file is valid, returning a reason if not."""

  file_extension = os.path.splitext(file)[1][1:]
  if not os.path.isfile(file):
    return ("Cannot find file %s." % file)

  elif not file_extension in languages():
    return ("Unexpected source file extension '%s'." % file_extension)

  else:
    return None

def arguments():
  """Parse command-line arguments"""

  parser = argparse.ArgumentParser()

  parser.add_argument('input_files', metavar='input-files', nargs='+',
    type = lambda x: (lambda r: x if r is None else parser.error(r))(validate_input_file(x)),
    help = 'source file to be translated/verified')

  parser.add_argument('--version', action='version',
    version='SMACK version ' + VERSION)

  noise_group = parser.add_mutually_exclusive_group()

  noise_group.add_argument('-q', '--quiet', action='store_true', default=False,
    help='enable quiet output')

  noise_group.add_argument('-v', '--verbose', action='store_true', default=False,
    help='enable verbose output')

  noise_group.add_argument('-d', '--debug', action="store_true", default=False,
    help='enable debugging output')

  noise_group.add_argument('--debug-only', metavar='MODULES', default=None,
    type=str, help='limit debugging output to given MODULES')

  parser.add_argument('-t', '--no-verify', action="store_true", default=False,
    help='perform only translation, without verification.')

  parser.add_argument('-w', '--error-file', metavar='FILE', default=None,
    type=str, help='save error trace/witness to FILE')


  frontend_group = parser.add_argument_group('front-end options')

  frontend_group.add_argument('-x', '--language', metavar='LANG',
    choices=frontends().keys(), default=None,
    help='Treat input files as having type LANG.')

  frontend_group.add_argument('-bc', '--bc-file', metavar='FILE', default=None,
    type=str, help='save initial LLVM bitcode to FILE')

  frontend_group.add_argument('--linked-bc-file', metavar='FILE', default=None,
    type=str, help=argparse.SUPPRESS)

  frontend_group.add_argument('--replay-harness', metavar='FILE', default='replay-harness.c',
    type=str, help=argparse.SUPPRESS)

  frontend_group.add_argument('--replay-exe-file', metavar='FILE', default='replay-exe',
    type=str, help=argparse.SUPPRESS)

  frontend_group.add_argument('-ll', '--ll-file', metavar='FILE', default=None,
    type=str, help='save final LLVM IR to FILE')

  frontend_group.add_argument('--clang-options', metavar='OPTIONS', default='',
    help='additional compiler arguments (e.g., --clang-options="-w -g")')


  translate_group = parser.add_argument_group('translation options')

  translate_group.add_argument('-bpl', '--bpl-file', metavar='FILE', default=None,
    type=str, help='save (intermediate) Boogie code to FILE')

  translate_group.add_argument('--no-memory-splitting', action="store_true", default=False,
    help='disable region-based memory splitting')

  translate_group.add_argument('--mem-mod', choices=['no-reuse', 'no-reuse-impls', 'reuse'], default='no-reuse-impls',
    help='select memory model (no-reuse=never reallocate the same address, reuse=reallocate freed addresses) [default: %(default)s]')

  translate_group.add_argument('--static-unroll', action="store_true", default=False,
    help='enable static LLVM loop unrolling pass as a preprocessing step')

  translate_group.add_argument('--pthread', action='store_true', default=False,
    help='enable support for pthread programs')

  translate_group.add_argument('--bit-precise', action="store_true", default=False,
    help='enable bit precision for non-pointer values')

  translate_group.add_argument('--timing-annotations', action="store_true", default=False,
    help='enable timing annotations')

  translate_group.add_argument('--bit-precise-pointers', action="store_true", default=False,
    help='enable bit precision for pointer values')

  translate_group.add_argument('--no-byte-access-inference', action="store_true", default=False,
    help='disable bit-precision-related optimizations with DSA')

  translate_group.add_argument('--entry-points', metavar='PROC', nargs='+',
    default=['main'], help='specify top-level procedures [default: %(default)s]')

  translate_group.add_argument('--memory-safety', action='store_true', default=False,
    help='enable memory safety checks')

  translate_group.add_argument('--only-check-valid-deref', action='store_true', default=False,
    help='only enable valid dereference checks')

  translate_group.add_argument('--only-check-valid-free', action='store_true', default=False,
    help='only enable valid free checks')

  translate_group.add_argument('--only-check-memleak', action='store_true', default=False,
    help='only enable memory leak checks')

  translate_group.add_argument('--integer-overflow', action='store_true', default=False,
    help='enable integer overflow checks')

  translate_group.add_argument('--float', action="store_true", default=False,
    help='enable bit-precise floating-point functions')

  translate_group.add_argument('--strings', action='store_true', default=False, help='enable support for string')

  verifier_group = parser.add_argument_group('verifier options')

  verifier_group.add_argument('--verifier',
    choices=['boogie', 'corral', 'symbooglix', 'duality', 'svcomp'], default='corral',
    help='back-end verification engine')

  verifier_group.add_argument('--unroll', metavar='N', default='1',
    type = lambda x: int(x) if int(x) > 0 else parser.error('Unroll bound has to be positive.'),
    help='loop/recursion unroll bound [default: %(default)s]')

  verifier_group.add_argument('--loop-limit', metavar='N', default='1', type=int,
    help='upper bound on minimum loop iterations [default: %(default)s]')

  verifier_group.add_argument('--context-bound', metavar='K', default='1', type=int,
    help='bound on the number of thread contexts in Corral [default: %(default)s]')

  verifier_group.add_argument('--verifier-options', metavar='OPTIONS', default='',
    help='additional verifier arguments (e.g., --verifier-options="/trackAllVars /staticInlining")')

  verifier_group.add_argument('--time-limit', metavar='N', default='1200', type=int,
    help='verifier time limit, in seconds [default: %(default)s]')

  verifier_group.add_argument('--max-violations', metavar='N', default='1', type=int,
    help='maximum reported assertion violations [default: %(default)s]')

  verifier_group.add_argument('--smackd', action="store_true", default=False,
    help='generate JSON-format output for SMACKd')

  verifier_group.add_argument('--svcomp-property', metavar='FILE', default=None,
    type=str, help='load SVCOMP property to check from FILE')

  verifier_group.add_argument('--modular', action="store_true", default=False,
    help='enable contracts-based modular deductive verification (uses Boogie)')

  verifier_group.add_argument('--replay', action="store_true", default=False,
    help='enable reply of error trace with test harness.')

  plugins_group = parser.add_argument_group('plugins')

  plugins_group.add_argument('--transform-bpl', metavar='COMMAND', default=None,
    type=str, help='transform generated Boogie code via COMMAND')

  plugins_group.add_argument('--transform-out', metavar='COMMAND', default=None,
    type=str, help='transform verifier output via COMMAND')

  args = parser.parse_args()

  if not args.bc_file:
    args.bc_file = temporary_file('a', '.bc', args)

  if not args.linked_bc_file:
    args.linked_bc_file = temporary_file('b', '.bc', args)

  if not args.bpl_file:
    args.bpl_file = 'a.bpl' if args.no_verify else temporary_file('a', '.bpl', args)

  if args.only_check_valid_deref or args.only_check_valid_free or args.only_check_memleak:
    args.memory_safety = True

  # TODO are we (still) using this?
  # with open(args.input_file, 'r') as f:
  #   for line in f.readlines():
  #     m = re.match('.*SMACK-OPTIONS:[ ]+(.*)$', line)
  #     if m:
  #       return args = parser.parse_args(m.group(1).split() + sys.argv[1:])

  return args

def target_selection(args):
  """Determine the target architecture based on flags and source files."""
  # TODO more possible clang flags that determine the target?
  if not re.search('-target', args.clang_options):
    src = args.input_files[0]
    if os.path.splitext(src)[1] == '.bc':
      ll = temporary_file(os.path.splitext(os.path.basename(src))[0], '.ll', args)
      try_command(['llvm-dis', '-o', ll, src])
      src = ll
    if os.path.splitext(src)[1] == '.ll':
      with open(src, 'r') as f:
        for line in f:
          triple = re.findall('^target triple = "(.*)"', line)
          if len(triple) > 0:
            args.clang_options += (" -target %s" % triple[0])
            break

def frontend(args):
  """Generate the LLVM bitcode file."""
  bitcodes = []
  libs = []
  
  def add_libs(lang):
    if lang in extra_libs():
      libs.append(extra_libs()[lang])

  if args.language:
    lang = languages()[args.language]
    add_libs(lang)
    frontend = frontends()[lang]
    for input_file in args.input_files:
      bitcodes.append(frontend(input_file,lang))
  
  else:
    for input_file in args.input_files:
      lang = languages()[os.path.splitext(input_file)[1][1:]]
      add_libs(lang)
      bitcode = frontends()[lang](input_file,args) 
      if bitcode is not None:
          bitcodes.append(bitcode)
  
  if len(bitcodes) == 0: # for specialized single-file frontends
      return

  return link_bc_files(bitcodes,libs,args)

def smack_root():
  return os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))

def smack_header_path():
  return os.path.join(smack_root(), 'share', 'smack', 'include')

def smack_headers():
  paths = []
  paths.append(smack_header_path())
  if args.memory_safety or args.integer_overflow:
    paths.append(os.path.join(smack_header_path(), 'string'))
  if args.float:
    paths.append(os.path.join(smack_header_path(), 'math'))
  return paths

def smack_lib():
  return os.path.join(smack_root(), 'share', 'smack', 'lib')

def default_clang_compile_command(args, lib = False):
  cmd = ['clang', '-c', '-emit-llvm', '-O0', '-g', '-gcolumn-info']
  cmd += map(lambda path: '-I' + path, smack_headers())
  cmd += args.clang_options.split()
  cmd += ['-DMEMORY_MODEL_' + args.mem_mod.upper().replace('-','_')]
  if args.memory_safety: cmd += ['-DMEMORY_SAFETY']
  if args.integer_overflow: cmd += (['-ftrapv'] if not lib else ['-DSIGNED_INTEGER_OVERFLOW_CHECK'])
  if args.float: cmd += ['-DFLOAT_ENABLED']
  if sys.stdout.isatty(): cmd += ['-fcolor-diagnostics']
  return cmd

def default_build_libs(args):
  """Generate LLVM bitcodes for SMACK libraries."""
  bitcodes = []
  libs = ['smack.c']

  if args.pthread:
    libs += ['pthread.c']

  if args.strings or args.memory_safety or args.integer_overflow:
    libs += ['string.c']

  if args.float:
    libs += ['math.c']
    libs += ['fenv.c']

  compile_command = default_clang_compile_command(args, True)
  for c in map(lambda c: os.path.join(smack_lib(), c), libs):
    bc = compile_to_bc(c,compile_command,args)
    bitcodes.append(bc)

  return bitcodes

def fortran_build_libs(args):
  """Generate FORTRAN-specific LLVM bitcodes for SMACK libraries."""

  bitcodes = []
  libs = ['smack.f90']

  compile_command = default_clang_compile_command(args)
  compile_command[0] = 'flang'

  for c in map(lambda c: os.path.join(smack_lib(), c), libs):
    bc = fortran_compile_to_bc(c,compile_command,args)
    bitcodes.append(bc)

  return bitcodes

def cplusplus_build_libs(args):
  """Generate C++ specific LLVM bitcodes for SMACK libraries."""

  bitcodes = []
  libs = ['smack.cpp']

  compile_command = default_clang_compile_command(args,True)
  compile_command[0] = 'clang++'

  for c in map(lambda c: os.path.join(smack_lib(), c), libs):
    bc = compile_to_bc(c,compile_command,args)
    bitcodes.append(bc)

  return bitcodes


def llvm_frontend(input_file, args):
  """Return LLVM IR file. Exists for symmetry with other frontends."""

  return input_file

def clang_frontend(input_file, args):
  """Generate LLVM IR from C-language source(s)."""

  compile_command = default_clang_compile_command(args)
  return compile_to_bc(input_file,compile_command,args)

def clang_plusplus_frontend(input_file, args):
  """Generate LLVM IR from C++ language source(s)."""
  compile_command = default_clang_compile_command(args)
  compile_command[0] = 'clang++'
  return compile_to_bc(input_file,compile_command,args)

def clang_objc_frontend(input_file, args):
  """Generate LLVM IR from Objective-C language source(s)."""

  compile_command = default_clang_compile_command(args)
  if sys.platform in ['linux', 'linux2']:
    objc_flags = try_command(['gnustep-config', '--objc-flags'])
    compile_command += objc_flags.split()
  elif sys.platform == 'darwin':
    sys.exit("Objective-C not yet supported on macOS")
  else:
    sys.exit("Objective-C not supported for this operating system.")
  return compile_to_bc(input_file,compile_command,args)

def d_frontend(input_file, args):
  """Generate Boogie code from D programming language source(s)."""

  # note: -g and -O0 are not used here. 
  # Right now, it works, and with these options, smack crashes.
  compile_command = ['ldc2', '-output-ll'] 
  compile_command += map(lambda path: '-I=' + path, smack_headers())
  args.entry_points = [ep if ep != 'main' else '_Dmain' for ep in args.entry_points]
  return compile_to_bc(input_file,compile_command,args)

def fortran_frontend(input_file, args):
  """Generate Boogie code from Fortran language source(s)."""

  #  For a fortran file that includes smack.f90 as a module,
  #  it will not compile unless the file 'smack.mod' exists
  #  in the working directory. 'smack.mod' is a build artifact
  #  of compiling smack.f90. Therefore, the solution is to 
  #  compile smack.f90 before the source files.
  fortran_build_libs(args)
  #  The result of this computation will be discarded when SMACK
  #  builds it's libraries later.

  # replace the default entry point with the fortran default 'MAIN_'
  args.entry_points = [ep if ep != 'main' else 'MAIN_' for ep in args.entry_points]

  compile_command = default_clang_compile_command(args)
  compile_command[0] = 'flang'

  return fortran_compile_to_bc(input_file,compile_command,args)

def boogie_frontend(input_file, args):
  """Pass Boogie code to the verifier."""
  if len(args.input_files) > 1: 
      raise RuntimeError("Expected a single Boogie file.")

  with open(args.bpl_file, 'a+') as out:
    with open(input_file) as f:
      out.write(f.read())

def json_compilation_database_frontend(input_file, args):
  """Generate Boogie code from a JSON compilation database."""

  if len(args.input_files) > 1:
    raise RuntimeError("Expected a single JSON compilation database.")

  output_flags = re.compile(r"-o ([^ ]*)[.]o\b")
  optimization_flags = re.compile(r"-O[1-9]\b")

  with open(input_file) as f:
    for cc in json.load(f):
      if 'objects' in cc:
        # TODO what to do when there are multiple linkings?
        bit_codes = map(lambda f: re.sub('[.]o$','.bc',f), cc['objects'])
        try_command(['llvm-link', '-o', args.bc_file] + bit_codes)
        try_command(['llvm-link', '-o', args.linked_bc_file, args.bc_file] + build_libs(args))

      else:
        out_file = output_flags.findall(cc['command'])[0] + '.bc'
        command = cc['command']
        command = output_flags.sub(r"-o \1.bc", command)
        command = optimization_flags.sub("-O0", command)
        command = command + " -emit-llvm"
        try_command(command.split(),cc['directory'], console=True)

  llvm_to_bpl(args)

def compile_to_bc(input_file, compile_command, args):
  """Compile a source file to LLVM IR."""
  bc = temporary_file(os.path.splitext(os.path.basename(input_file))[0], '.bc', args)
  try_command(compile_command + ['-o', bc, input_file], console=True)
  return bc

def fortran_compile_to_bc(input_file, compile_command, args):
  """Compile a FORTRAN source file to LLVM IR."""

  #  This method only exists as a hack to get flang to work
  #  with SMACK. When we update to the latest flang on LLVM 5,
  #  this method will no longer be necessary. The hack is 
  #  self-contained in this method.

  #  The Debug Info Version in flang is incompatible with 
  #  the version that clang uses. The workaround is to use
  #  sed to change the file so llvm-link gives a warning
  #  and not an error. 

  # compile to human-readable format in order to tweak the IR
  compile_command[1] = '-S'
  ll = temporary_file(os.path.splitext(os.path.basename(input_file))[0], '.ll', args)
  try_command(compile_command + ['-o', ll, input_file], console=True)
  # change the throw level of 'Debug Info Version' from error to warning in the IR
  try_command(['sed', '-i', 's/i32 1, !\"Debug Info Version\"/i32 2, !\"Debug Info Version\"/g', ll])
  try_command(['llvm-as', ll])
  try_command(['rm', ll])
  bc = '.'.join(ll.split('.')[:-1] + ['bc'])
  return bc 
 
def link_bc_files(bitcodes, libs, args):
  """Link generated LLVM bitcode and relevant smack libraries."""

  smack_libs = default_build_libs(args)
  for build_lib in libs:
    smack_libs += build_lib(args)

  try_command(['llvm-link', '-o', args.bc_file] + bitcodes)
  try_command(['llvm-link', '-o', args.linked_bc_file, args.bc_file] + smack_libs)
  llvm_to_bpl(args)

def llvm_to_bpl(args):
  """Translate the LLVM bitcode file to a Boogie source file."""

  cmd = ['llvm2bpl', args.linked_bc_file, '-bpl', args.bpl_file]
  cmd += ['-warnings']
  cmd += ['-source-loc-syms']
  for ep in args.entry_points:
    cmd += ['-entry-points', ep]
  if args.debug: cmd += ['-debug']
  if args.debug_only: cmd += ['-debug-only', args.debug_only]
  if args.ll_file: cmd += ['-ll', args.ll_file]
  if "impls" in args.mem_mod:cmd += ['-mem-mod-impls']
  if args.static_unroll: cmd += ['-static-unroll']
  if args.bit_precise: cmd += ['-bit-precise']
  if args.timing_annotations: cmd += ['-timing-annotations']
  if args.bit_precise_pointers: cmd += ['-bit-precise-pointers']
  if args.no_byte_access_inference: cmd += ['-no-byte-access-inference']
  if args.no_memory_splitting: cmd += ['-no-memory-splitting']
  if args.memory_safety: cmd += ['-memory-safety']
  if args.integer_overflow: cmd += ['-integer-overflow']
  if args.float: cmd += ['-float']
  if args.modular: cmd += ['-modular']
  try_command(cmd, console=True)
  annotate_bpl(args)
  property_selection(args)
  transform_bpl(args)

def procedure_annotation(name, args):
  if name in args.entry_points:
    return "{:entrypoint}"
  elif args.modular and re.match("|".join(inlined_procedures()).replace("$","\$"), name):
    return "{:inline 1}"
  elif (not args.modular) and (args.verifier == 'boogie' or args.float):
    return ("{:inline %s}" % args.unroll)
  else:
    return ""

def annotate_bpl(args):
  """Annotate the Boogie source file with additional metadata."""

  proc_decl = re.compile('procedure\s+([^\s(]*)\s*\(')

  with open(args.bpl_file, 'r+') as f:
    bpl = "// generated by SMACK version %s for %s\n" % (VERSION, args.verifier)
    bpl += "// via %s\n\n" % " ".join(sys.argv)
    bpl += proc_decl.sub(lambda m: ("procedure %s %s(" % (procedure_annotation(m.group(1), args), m.group(1))), f.read())
    f.seek(0)
    f.truncate()
    f.write(bpl)

def property_selection(args):
  selected_props = []
  if args.only_check_valid_deref:
    selected_props.append('valid_deref')
  elif args.only_check_valid_free:
    selected_props.append('valid_free')
  elif args.only_check_memleak:
    selected_props.append('valid_memtrack')

  def replace_assertion(m):
    if len(selected_props) > 0:
      if m.group(2) and m.group(3) in selected_props:
        attrib = m.group(2)
        expr = m.group(4)
      else:
        attrib = ''
        expr = 'true'
      return m.group(1) + attrib + expr + ";"
    else:
      return m.group(0)

  with open(args.bpl_file, 'r+') as f:
    lines = f.readlines()
    f.seek(0)
    f.truncate()
    for line in lines:
      line = re.sub(r'^(\s*assert\s*)({:(.+)})?(.+);', replace_assertion, line)
      f.write(line)

def transform_bpl(args):
  if args.transform_bpl:
    with open(args.bpl_file, 'r+') as bpl:
      old = bpl.read()
      bpl.seek(0)
      bpl.truncate()
      tx = subprocess.Popen(shlex.split(args.transform_bpl), stdin=subprocess.PIPE, stdout=bpl)
      tx.communicate(input = old)

def transform_out(args, old):
  out = old
  if args.transform_out:
    tx = subprocess.Popen(shlex.split(args.transform_out), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = tx.communicate(input = old)
  return out

def verification_result(verifier_output):
  if re.search(r'[1-9]\d* time out|Z3 ran out of resources|timed out|ERRORS_TIMEOUT', verifier_output):
    return 'timeout'
  elif re.search(r'[1-9]\d* verified, 0 errors?|no bugs|NO_ERRORS_NO_TIMEOUT', verifier_output):
    return 'verified'
  elif re.search(r'\d* verified, [1-9]\d* errors?|can fail|ERRORS_NO_TIMEOUT', verifier_output):
    if re.search(r'ASSERTION FAILS assert {:valid_deref}', verifier_output):
      return 'invalid-deref'
    elif re.search(r'ASSERTION FAILS assert {:valid_free}', verifier_output):
      return 'invalid-free'
    elif re.search(r'ASSERTION FAILS assert {:valid_memtrack}', verifier_output):
      return 'invalid-memtrack'
    elif re.search(r'ASSERTION FAILS assert {:overflow}', verifier_output):
      return 'overflow'
    else:
      listCall = re.findall(r'\(CALL .+\)', verifier_output)
      if len(listCall) > 0 and re.search(r'free_', listCall[len(listCall)-1]):
        return 'invalid-free'
      else:
        return 'error'
  else:
    return 'unknown'

def verify_bpl(args):
  """Verify the Boogie source file with a back-end verifier."""

  if args.verifier == 'svcomp':
    verify_bpl_svcomp(args)
    return

  elif args.verifier == 'boogie' or args.modular:
    command = ["boogie"]
    command += [args.bpl_file]
    command += ["/nologo", "/noinfer", "/doModSetAnalysis"]
    command += ["/timeLimit:%s" % args.time_limit]
    command += ["/errorLimit:%s" % args.max_violations]
    if not args.modular:
      command += ["/loopUnroll:%d" % args.unroll]

  elif args.verifier == 'corral':
    command = ["corral"]
    command += [args.bpl_file]
    command += ["/tryCTrace", "/noTraceOnDisk", "/printDataValues:1"]
    command += ["/k:%d" % args.context_bound]
    command += ["/useProverEvaluate"]
    command += ["/timeLimit:%s" % args.time_limit]
    command += ["/cex:%s" % args.max_violations]
    command += ["/maxStaticLoopBound:%d" % args.loop_limit]
    command += ["/recursionBound:%d" % args.unroll]
	
  elif args.verifier == 'symbooglix':
    command = ['symbooglix']
    command += [args.bpl_file]
    command += ["--file-logging=0"]
    command += ["--entry-points=%s" % ",".join(args.entry_points)]
    command += ["--timeout=%d" % args.time_limit]
    command += ["--max-loop-depth=%d" % args.unroll]

  else:
    # Duality!
    command = ["corral", args.bpl_file]
    command += ["/tryCTrace", "/noTraceOnDisk", "/useDuality", "/oldStratifiedInlining"]
    command += ["/recursionBound:1073741824", "/k:1"]

  if (args.bit_precise or args.float) and args.verifier != 'symbooglix':
    x = "bopt:" if args.verifier != 'boogie' else ""
    command += ["/%sproverOpt:OPTIMIZE_FOR_BV=true" % x]
    command += ["/%sboolControlVC" % x]

  if args.verifier_options:
    command += args.verifier_options.split()

  verifier_output = try_command(command, timeout=args.time_limit)
  verifier_output = transform_out(args, verifier_output)
  result = verification_result(verifier_output)

  if args.smackd:
    print smackdOutput(verifier_output)

  elif result == 'verified':
    print results(args)[result]

  else:
    if result == 'error' or result == 'invalid-deref' or result == 'invalid-free' or result == 'invalid-memtrack' or result == 'overflow':
      error = error_trace(verifier_output, args)

      if args.error_file:
        with open(args.error_file, 'w') as f:
          f.write(error)

      if not args.quiet:
        print error

      if args.replay:
         replay_error_trace(verifier_output, args)

    sys.exit(results(args)[result])

def error_step(step):
  FILENAME = '[\w#$~%.\/-]*'
  step = re.match("(\s*)(%s)\((\d+),\d+\): (.*)" % FILENAME, step)
  if step:
    if re.match('.*[.]bpl$', step.group(2)):
      line_no = int(step.group(3))
      message = step.group(4)
      if re.match('.*\$bb\d+.*', message):
        message = ""
      with open(step.group(2)) as f:
        for line in f.read().splitlines(True)[line_no:line_no+10]:
          src = re.match(".*{:sourceloc \"(%s)\", (\d+), (\d+)}" % FILENAME, line)
          if src:
            return "%s%s(%s,%s): %s" % (step.group(1), src.group(1), src.group(2), src.group(3), message)
    else:
      return step.group(0)
  else:
    return None

def error_trace(verifier_output, args):
  trace = ""
  for line in verifier_output.splitlines(True):
    step = error_step(line)
    if step:
      m = re.match('(.*): [Ee]rror [A-Z0-9]+: (.*)', step)
      if m:
        trace += "%s: %s\nExecution trace:\n" % (m.group(1), m.group(2))
      else:
        trace += ('' if step[0] == ' ' else '    ') + step + "\n"

  return trace

def smackdOutput(corralOutput):
  FILENAME = '[\w#$~%.\/-]+'

  passedMatch = re.search('Program has no bugs', corralOutput)
  if passedMatch:
    json_data = {
      'verifier': 'corral',
      'passed?': True
    }

  else:
    traces = []
    filename = ''
    lineno = 0
    colno = 0
    threadid = 0
    desc = ''
    for traceLine in corralOutput.splitlines(True):
      traceMatch = re.match('(' + FILENAME + ')\((\d+),(\d+)\): Trace: Thread=(\d+)  (\((.*)\))?$', traceLine)
      traceAssumeMatch = re.match('(' + FILENAME + ')\((\d+),(\d+)\): Trace: Thread=(\d+)  (\((\s*\w+\s*=\s*\w+\s*)\))$', traceLine)
      errorMatch = re.match('(' + FILENAME + ')\((\d+),(\d+)\): (error .*)$', traceLine)
      if traceMatch:
        filename = str(traceMatch.group(1))
        lineno = int(traceMatch.group(2))
        colno = int(traceMatch.group(3))
        threadid = int(traceMatch.group(4))
        desc = str(traceMatch.group(6))
        assm = ''
        if traceAssumeMatch:
          assm = str(traceAssumeMatch.group(6))
          #Remove bitvector indicator from trace assumption
          assm = re.sub(r'=(\s*\d+)bv\d+', r'=\1', assm)
        trace = { 'threadid': threadid,
                  'file': filename,
                  'line': lineno,
                  'column': colno,
                  'description': '' if desc == 'None' else desc,
                  'assumption': assm }
        traces.append(trace)
      elif errorMatch:
        filename = str(errorMatch.group(1))
        lineno = int(errorMatch.group(2))
        colno = int(errorMatch.group(3))
        desc = str(errorMatch.group(4))

    failsAt = { 'file': filename, 'line': lineno, 'column': colno, 'description': desc }

    json_data = {
      'verifier': 'corral',
      'passed?': False,
      'failsAt': failsAt,
      'threadCount': 1,
      'traces': traces
    }
  json_string = json.dumps(json_data)
  return json_string

def main():
  try:
    global args
    args = arguments()

    target_selection(args)

    if not args.quiet:
      print "SMACK program verifier version %s" % VERSION

    frontend(args)

    if args.no_verify:
      if not args.quiet:
        print "SMACK generated %s" % args.bpl_file
    else:
      verify_bpl(args)

  except KeyboardInterrupt:
    sys.exit("SMACK aborted by keyboard interrupt.")

  finally:
    remove_temp_files()
