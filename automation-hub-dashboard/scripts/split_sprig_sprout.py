# Run from automation-hub-dashboard/:  python3 scripts/split_sprig_sprout.py   (needs Pillow)
"""Split Sprig's sprite into a body layer and a sprout layer, exactly.

Every pixel goes to exactly one layer, so the two stacked reproduce
sprig-production-poses-v4.png bit for bit (asserted). The sprout is what
the head cannot reach: a flood fill from inside the face, walled off at the
stem and by a short barrier just above the head socket.
"""
from PIL import Image
import json, sys
SRC = "docs/nexus-pet/sprig-production-poses-v4.png"   # the approved art, kept out of the build
src = Image.open(SRC).convert("RGBA")
W, H = src.size; CELL = 620; px = src.load()
green = lambda c: c[3] > 40 and c[1] > c[0] + 25 and c[1] > c[2] + 25
N8 = [(1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,-1),(1,-1),(-1,1)]
seeds = {0: (300, 420), 1: (330, 400), 2: (360, 380), 3: (300, 400)}
leaf_mask, pivots = set(), {}
for i in range(4):
    ox = i * CELL
    cand = {(x, y) for y in range(0, 280) for x in range(ox, ox + CELL) if green(px[x, y])}
    top = min(cand, key=lambda p: p[1])
    comp, stack = {top}, [top]
    while stack:
        x, y = stack.pop()
        for dx, dy in N8:
            q = (x+dx, y+dy)
            if q in cand and q not in comp: comp.add(q); stack.append(q)
    cut = max(p[1] for p in comp)
    stem_x = sum(p[0] for p in comp if p[1] >= cut - 2) / max(1, sum(1 for p in comp if p[1] >= cut - 2))
    walls = {(x+dx, y+dy) for (x, y) in comp for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    walls |= {(int(stem_x) + dx, y) for dx in range(-70, 71) for y in range(cut - 5, cut - 1)}   # above the socket
    sx, sy = seeds[i]; seed = (ox + sx, sy)
    head, stack = {seed}, [seed]
    while stack:
        x, y = stack.pop()
        for dx, dy in N8:
            q = (x+dx, y+dy)
            if ox <= q[0] < ox + CELL and 0 <= q[1] < H and q not in head and q not in walls and px[q][3] > 0:
                head.add(q); stack.append(q)
    region = {(x, y) for y in range(0, cut) for x in range(ox, ox + CELL) if px[x, y][3] > 0 and (x, y) not in head}
    region |= comp
    joint = cut - 14                       # the stem flexes here; its base, the socket and rim stay put
    region = {p for p in region if p[1] < joint}
    leaf_mask |= region
    pivots[i] = {"x": round((stem_x - ox) / CELL * 100, 2), "y": round(joint / H * 100, 2), "cut": cut, "pixels": len(region)}
    print(f"pose {i+1}: sprout layer {len(region)} px, stem base {pivots[i]['x']}% {pivots[i]['y']}%")
body = src.copy(); leaf = Image.new("RGBA", src.size, (0, 0, 0, 0))
bp, lp = body.load(), leaf.load()
for (x, y) in leaf_mask:
    lp[x, y] = px[x, y]; bp[x, y] = (0, 0, 0, 0)
rec = body.copy(); rec.alpha_composite(leaf)
assert rec.tobytes() == src.tobytes(), "split is not exact"
body.save("public/nexus-pet-concepts/sprig-body-v5.png", optimize=True)
leaf.save("public/nexus-pet-concepts/sprig-sprout-v5.png", optimize=True)
json.dump(pivots, open(sys.argv[1], "w") if len(sys.argv) > 1 else sys.stdout, indent=1)
print("exact split written")
