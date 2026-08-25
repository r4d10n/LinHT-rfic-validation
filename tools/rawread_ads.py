#!/usr/bin/env python3
"""Read ADS hpeesofsim MDS raw files (-r out.raw). Returns list of plots: dict(name, vars[list], data{var: np.array}).
CLI: rawread.py file.raw [--csv] prints a summary / CSV of each plot."""
import sys, struct, re
import numpy as np

def read_raw(path):
    b = open(path, 'rb').read()
    plots, pos = [], 0
    while True:
        i = b.find(b'Plotname:', pos)
        if i < 0: break
        j = b.find(b'Binary:', i)
        hdr = b[i:j].decode('latin1')
        name = hdr.splitlines()[0][len('Plotname:'):].strip()
        nvar = int(re.search(r'No\. Variables:\s*(\d+)', hdr).group(1))
        npts_m = re.search(r'No\. Points:\s*(\d+)', hdr)
        names = re.findall(r'^\s*\d+\s+(\S+)\s+\S+.*$', hdr.split('Variables:')[-1], re.M)
        cplx = 'complex' in hdr.split('Flags:')[1].splitlines()[0]
        # Binary:8:<order marker>\n then data
        k = b.find(b'\n', j) + 1
        width = int(b[j:k].split(b':')[1])
        # find next plot or EOF
        nxt = b.find(b'Plotname:', k)
        end = len(b) if nxt < 0 else nxt
        # strip trailing newline(s)/text before next Plotname
        raw = b[k:end]
        per = nvar * width * (2 if cplx else 1)
        npts = int(npts_m.group(1)) if npts_m else len(raw) // per
        # MDS order marker: 'ABCDEFG' = little-endian, 'GFEDCBA' = big-endian
        parts = b[j:k].rstrip(b'\n').split(b':')
        order_mark = parts[2] if len(parts) >= 3 else b'ABCDEFG'
        dt = ('<' if order_mark.startswith(b'A') else '>') + \
             ('f8' if width == 8 else 'f4')
        arr = np.frombuffer(raw[:npts * per], dtype=dt)
        if cplx:
            arr = arr.reshape(npts, nvar, 2); arr = arr[..., 0] + 1j * arr[..., 1]
        else:
            arr = arr.reshape(npts, nvar)
        plots.append({'name': name, 'vars': names, 'data': {n: arr[:, c] for c, n in enumerate(names)}})
        pos = end
    return plots

if __name__ == '__main__':
    csv = '--csv' in sys.argv
    for p in read_raw(sys.argv[1]):
        print('#', p['name'], p['vars'], len(next(iter(p['data'].values()))))
        if csv:
            print(','.join(p['vars']))
            cols = [p['data'][v] for v in p['vars']]
            for r in zip(*cols): print(','.join('%.6g' % (x.real if abs(x.imag) < 1e-300 else x) for x in r))
