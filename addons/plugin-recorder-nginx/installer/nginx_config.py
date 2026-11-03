"""Conservative structural Nginx parser; never edits a file globally by regex."""
from pathlib import Path
import re

class Node:
    def __init__(self, words, start, opening=None):
        self.words=words; self.start=start; self.opening=opening; self.end=None; self.children=[]

def tokens(text):
    i=0
    while i<len(text):
        if text[i].isspace(): i+=1; continue
        if text[i]=='#':
            end=text.find('\n',i);i=len(text) if end<0 else end+1;continue
        start=i
        if text[i] in '{};': yield text[i],i,i+1;i+=1;continue
        word='';quote=None
        while i<len(text):
            ch=text[i]
            if ch=='\\':
                if i+1>=len(text):raise ValueError('Trailing Nginx escape')
                word+=text[i+1];i+=2;continue
            if quote:
                if ch==quote:quote=None
                else:word+=ch
                i+=1;continue
            if ch in ('"',"'"):quote=ch;i+=1;continue
            if ch=='$' and i+1<len(text) and text[i+1]=='{':
                end=text.find('}',i+2)
                if end<0:raise ValueError('Unclosed Nginx variable')
                word+=text[i:end+1];i=end+1;continue
            if ch.isspace() or ch in '{};#':break
            word+=ch;i+=1
        if quote:raise ValueError('Unclosed Nginx quote')
        if i==start:raise ValueError('Cannot tokenize Nginx')
        yield word,start,i

def parse(text):
    root=Node([],0);root.end=len(text);stack=[root];pending=[];start=None
    for value,a,b in tokens(text):
        if value=='}':
            if pending or len(stack)==1:raise ValueError('Unbalanced Nginx braces/directive')
            stack.pop().end=a;continue
        if value in ('{',';'):
            if not pending:raise ValueError('Empty Nginx directive')
            node=Node(pending,start,a if value=='{' else None);stack[-1].children.append(node)
            if value=='{':stack.append(node)
            else:node.end=b
            pending=[];start=None;continue
        if start is None:start=a
        pending.append(value)
    if pending or len(stack)!=1:raise ValueError('Incomplete Nginx configuration')
    return root

def servers(text,domain):
    root=parse(text);result=[]
    def walk(node):
        for child in node.children:
            if child.words[0]=='server' and child.opening is not None:
                names=[x.words[1:] for x in child.children if x.words[0]=='server_name']
                listeners=[x.words[1:] for x in child.children if x.words[0]=='listen']
                if any(domain in nameset for nameset in names) and any('ssl' in words and (words[0]=='443' or words[0].endswith(':443')) for words in listeners):result.append(child)
            else:walk(child)
    walk(root);return result

def strip_managed(text):
    begin='# BEGIN MATRIX RECORDER NGINX';end='# END MATRIX RECORDER NGINX'
    if begin not in text and end not in text:return text
    if text.count(begin)!=1 or text.count(end)!=1:raise ValueError('Ambiguous managed recorder markers')
    a=text.index(begin);b=text.index(end)
    if a>b:raise ValueError('Reversed managed recorder markers')
    start=text.rfind('\n',0,a)
    if start<0:start=0
    stop=text.find('\n',b)
    stop=len(text) if stop<0 else stop+1
    return text[:start]+text[stop:]

def inject(text,domain,snippet):
    cleaned=strip_managed(text);found=servers(cleaned,domain)
    if len(found)!=1:raise ValueError('Expected one exact TLS server_name '+domain+' in selected file')
    server=found[0]
    for node in server.children:
        if node.words[0]=='location':
            pattern=node.words[-1]
            if any(pattern.rstrip('/')==p or pattern.startswith(p+'/') for p in ['/recording','/recordings','/modules/recorder-nginx']):raise ValueError('Existing conflicting Nginx location '+pattern)
    block='\n    # BEGIN MATRIX RECORDER NGINX\n    include '+str(snippet)+';\n    # END MATRIX RECORDER NGINX\n'
    return cleaned[:server.end]+block+cleaned[server.end:]

def active_target(dump,domain,target):
    """Require the exact file to be loaded and reject duplicate matching TLS vhosts."""
    headers=list(re.finditer(r'^# configuration file (.+):\s*$',dump,re.M))
    matches=[];loaded=set()
    for i,h in enumerate(headers):
        path=Path(h.group(1)).resolve();loaded.add(path)
        section=dump[h.end():headers[i+1].start() if i+1<len(headers) else len(dump)]
        matches.extend([path]*len(servers(section,domain)))
    if Path(target).resolve() not in loaded:raise ValueError('Configured vhost is not loaded by nginx -T; select the enabled authoritative file')
    if matches!=[Path(target).resolve()]:raise ValueError('Duplicate or ambiguous active TLS server_name; resolve conflicting enabled sites before installing')
