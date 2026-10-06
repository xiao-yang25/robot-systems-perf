"""Test-only installed-child I/O sentinel; never included in the product wheel."""
import json
from pathlib import Path


GUARD_CODE = '''import atexit,builtins,io,json,sys
from pathlib import Path
from perfkit.platform_probe import _Probe
guard_path=Path(sys.argv[1])
guard_state={'installed':True,'finished':False,'accesses':[]}
def save_guard():
 guard_path.write_text(json.dumps(guard_state)+'\\n')
def thermal(source):
 text=str(source)
 return '/sys/class/thermal' in text or ('/sys/class/hwmon' in text and Path(text).name.startswith('temp'))
def check(source,operation):
 if thermal(source):
  guard_state['accesses'].append({'operation':operation,'source':str(source)})
  save_guard()
  raise AssertionError('temperature interface '+operation+' attempted')
read,children=_Probe.read,_Probe.children
def checked_read(probe,source,*args):
 check(source,'read')
 return read(probe,source,*args)
def checked_children(probe,source):
 check(source,'discovery')
 return children(probe,source)
_Probe.read,_Probe.children=checked_read,checked_children
opened,builtin_open,path_open,globbed,listed=io.open,builtins.open,Path.open,Path.glob,Path.iterdir
def checked_open(file,*args,**kwargs):
 if isinstance(file,(str,Path)): check(file,'open')
 return opened(file,*args,**kwargs)
def checked_builtin_open(file,*args,**kwargs):
 if isinstance(file,(str,Path)): check(file,'open')
 return builtin_open(file,*args,**kwargs)
def checked_path_open(path,*args,**kwargs):
 check(path,'open')
 return path_open(path,*args,**kwargs)
def checked_glob(path,pattern):
 check(path/str(pattern),'glob')
 return globbed(path,pattern)
def checked_iterdir(path):
 check(path,'listing')
 return listed(path)
io.open,builtins.open,Path.open,Path.glob,Path.iterdir=checked_open,checked_builtin_open,checked_path_open,checked_glob,checked_iterdir
def finish_guard():
 guard_state['finished']=True
 save_guard()
save_guard()
atexit.register(finish_guard)
'''


def guarded_command(command, python, evidence):
    # Run the installed console script or existing fault-injection code under
    # the same selected interpreter, without importing the source checkout.
    # Isolate sentinel globals from the wrapped test's own fault-injection
    # helpers; both may bind names such as read or original.
    code = 'import sys\nexec(' + repr(GUARD_CODE) + ', {})\n' + '''import runpy
command=sys.argv[2:]
if len(command)>2 and command[1]=='-c':
 sys.argv=['-c',*command[3:]]
 exec(compile(command[2],'<controlled-installed-child>','exec'))
else:
 sys.argv=command
 runpy.run_path(command[0],run_name='__main__')
'''
    return [str(python), '-c', code, str(evidence), *map(str, command)]


def assert_temperature_guard(evidence):
    value = json.loads(Path(evidence).read_text())
    assert value['installed'] and value['finished'], value
    assert value['accesses'] == [], value
