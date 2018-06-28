import os

import yaml
from jinja2 import Template

from copy import deepcopy

def merge(a, b):
    if not a:
        a = dict()

    if not b:
        b = dict()

    if isinstance(b, dict) and isinstance(a, dict):
        a_and_b = set(a.keys()) & set(b.keys())
        every_key = set(a.keys()) | set(b.keys())
        return {k: merge(a[k], b[k]) if k in a_and_b else
                   deepcopy(a[k] if k in a else b[k]) for k in every_key}

    return deepcopy(b)

def lrchop(s, b='', e=''):
    if s.startswith(b) and len(b)>0:
        s = s[len(b):]
    if s.endswith(e) and len(e)>0:
        s = s[:-len(e)]
    return s

def relative_filename(fullpath_filename, rootpath='/'):
    r = lrchop(fullpath_filename,rootpath)
    return r.lstrip('/') if r and r[0]=='/' else r

def absolute_filename(s, rootpath='.'):
    return s if s.startswith('/') else '{}/{}'.format(rootpath,s)

def breadcrumb_path(fullpath, rootpath='/'):
    return '.' + relative_filename(fullpath, rootpath).replace('/','.')

def get_project_files(ext, rootpath='.', exclude_dirs=[], ignore_dir_with_file='', relative_path=True):
    top  = rootpath

    lst = list()
    for root, dirs, files in os.walk(top, topdown=True):
        for d in exclude_dirs:
            if d in dirs:
                dirs.remove(d)

        if ignore_dir_with_file in files:
            dirs[:] = []
            next
        else:
            for file in files:
                if file.endswith(ext):
                    f = os.path.join(root, file)
                    lst.append(relative_filename(f,rootpath) if relative_path else f)

    return lst

def pretty_print(metadata):
    print(yaml.dump(metadata, indent=2, default_flow_style=False))

def render(m, passes=10):
    doc = {}
    for k in m.keys():
        doc[k] = yaml.dump(m[k])

    for k in doc.keys():
        for i in range(passes):
            template = Template(doc[k])
            doc[k] = template.render(yaml.load(doc[k]))

    d = {}
    for k in doc.keys():
        d[k] = yaml.load(doc[k])

    return d

#get_project_files(ext='metadata.yml', ignore_dir_with_file='metadata.ignore.yml', relative_path=False)
#get_project_files(ext='.ipynb', exclude_dirs=['.ipynb_checkpoints'])