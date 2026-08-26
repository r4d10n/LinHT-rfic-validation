import re, os, sys
os.chdir("/tmp/spdk")

def resolve_conditionals(text):
    lines = text.splitlines()
    out = []
    stack = []
    for line in lines:
        s = line.strip().lower()
        if s.startswith(".if ") or s.startswith(".if("):
            stack.append(True)
            continue
        if s.startswith(".elseif"):
            continue
        if s.startswith(".else"):
            if stack:
                stack[-1] = False
            continue
        if s.startswith(".endif"):
            if stack:
                stack.pop()
            continue
        skip = any(stack)
        if not skip:
            out.append(line)
    return "\n".join(out)

for fn in ["capacitors_mod.lib", "resistors_mod.lib", "sg13g2_moslv_mod.lib", "diodes.lib"]:
    if not os.path.exists(fn):
        continue
    text = open(fn).read()
    result = resolve_conditionals(text)
    open(fn, "w").write(result)
    print(f"{fn}: {len(text)} -> {len(result)} bytes")

print("done")
