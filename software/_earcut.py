"""
Earcut polygon triangulation — pure Python, no numpy.
Ported from mapbox/earcut.js by the earcut PyPI package authors,
numpy dependency removed (we only use dim=2).
"""
import math


def earcut(data, holeIndices=None, dim=None):
    dim = dim or 2
    hasHoles = holeIndices and len(holeIndices)
    outerLen = holeIndices[0] * dim if hasHoles else len(data)
    outerNode = linkedList(data, 0, outerLen, dim, True)
    triangles = []

    if not outerNode:
        return triangles

    minX = minY = maxX = maxY = size = None

    if hasHoles:
        outerNode = eliminateHoles(data, holeIndices, outerNode, dim)

    if len(data) > 80 * dim:
        minX = maxX = data[0]
        minY = maxY = data[1]
        for i in range(dim, outerLen, dim):
            x, y = data[i], data[i + 1]
            if x < minX: minX = x
            if y < minY: minY = y
            if x > maxX: maxX = x
            if y > maxY: maxY = y
        size = max(maxX - minX, maxY - minY)

    earcutLinked(outerNode, triangles, dim, minX, minY, size)
    return triangles


def linkedList(data, start, end, dim, clockwise):
    last = None
    if clockwise == (signedArea(data, start, end, dim) > 0):
        for i in range(start, end, dim):
            last = insertNode(i, data[i], data[i + 1], last)
    else:
        for i in reversed(range(start, end, dim)):
            last = insertNode(i, data[i], data[i + 1], last)
    if last and equals(last, last.next):
        removeNode(last)
        last = last.next
    return last


def filterPoints(start, end=None):
    if not start:
        return start
    if not end:
        end = start
    p = start
    again = True
    while again or p != end:
        again = False
        if not p.steiner and (equals(p, p.next) or area(p.prev, p, p.next) == 0):
            removeNode(p)
            p = end = p.prev
            if p == p.next:
                return None
            again = True
        else:
            p = p.next
    return end


def earcutLinked(ear, triangles, dim, minX, minY, size, _pass=None):
    if not ear:
        return
    if not _pass and size:
        indexCurve(ear, minX, minY, size)
    stop = ear
    while ear.prev != ear.next:
        prev = ear.prev
        next = ear.next
        if (isEarHashed(ear, minX, minY, size) if size else isEar(ear)):
            triangles.append(prev.i // dim)
            triangles.append(ear.i // dim)
            triangles.append(next.i // dim)
            removeNode(ear)
            ear = next.next
            stop = next.next
            continue
        ear = next
        if ear == stop:
            if not _pass:
                earcutLinked(filterPoints(ear), triangles, dim, minX, minY, size, 1)
            elif _pass == 1:
                ear = cureLocalIntersections(ear, triangles, dim)
                earcutLinked(ear, triangles, dim, minX, minY, size, 2)
            elif _pass == 2:
                splitEarcut(ear, triangles, dim, minX, minY, size)
            break


def isEar(ear):
    a, b, c = ear.prev, ear, ear.next
    if area(a, b, c) >= 0:
        return False
    p = ear.next.next
    while p != ear.prev:
        if pointInTriangle(a.x, a.y, b.x, b.y, c.x, c.y, p.x, p.y) and area(p.prev, p, p.next) >= 0:
            return False
        p = p.next
    return True


def isEarHashed(ear, minX, minY, size):
    a, b, c = ear.prev, ear, ear.next
    if area(a, b, c) >= 0:
        return False
    minTX = (a.x if a.x < c.x else c.x) if a.x < b.x else (b.x if b.x < c.x else c.x)
    minTY = (a.y if a.y < c.y else c.y) if a.y < b.y else (b.y if b.y < c.y else c.y)
    maxTX = (a.x if a.x > c.x else c.x) if a.x > b.x else (b.x if b.x > c.x else c.x)
    maxTY = (a.y if a.y > c.y else c.y) if a.y > b.y else (b.y if b.y > c.y else c.y)
    minZ = zOrder(minTX, minTY, minX, minY, size)
    maxZ = zOrder(maxTX, maxTY, minX, minY, size)
    p = ear.nextZ
    while p and p.z <= maxZ:
        if p != ear.prev and p != ear.next and pointInTriangle(a.x, a.y, b.x, b.y, c.x, c.y, p.x, p.y) and area(p.prev, p, p.next) >= 0:
            return False
        p = p.nextZ
    p = ear.prevZ
    while p and p.z >= minZ:
        if p != ear.prev and p != ear.next and pointInTriangle(a.x, a.y, b.x, b.y, c.x, c.y, p.x, p.y) and area(p.prev, p, p.next) >= 0:
            return False
        p = p.prevZ
    return True


def cureLocalIntersections(start, triangles, dim):
    do = True
    p = start
    while do or p != start:
        do = False
        a, b = p.prev, p.next.next
        if not equals(a, b) and intersects(a, p, p.next, b) and locallyInside(a, b) and locallyInside(b, a):
            triangles.append(a.i // dim)
            triangles.append(p.i // dim)
            triangles.append(b.i // dim)
            removeNode(p)
            removeNode(p.next)
            p = start = b
        p = p.next
    return p


def splitEarcut(start, triangles, dim, minX, minY, size):
    do = True
    a = start
    while do or a != start:
        do = False
        b = a.next.next
        while b != a.prev:
            if a.i != b.i and isValidDiagonal(a, b):
                c = splitPolygon(a, b)
                a = filterPoints(a, a.next)
                c = filterPoints(c, c.next)
                earcutLinked(a, triangles, dim, minX, minY, size)
                earcutLinked(c, triangles, dim, minX, minY, size)
                return
            b = b.next
        a = a.next


def eliminateHoles(data, holeIndices, outerNode, dim):
    queue = []
    _len = len(holeIndices)
    for i in range(_len):
        start = holeIndices[i] * dim
        end = holeIndices[i + 1] * dim if i < _len - 1 else len(data)
        lst = linkedList(data, start, end, dim, False)
        if lst == lst.next:
            lst.steiner = True
        queue.append(getLeftmost(lst))
    queue.sort(key=lambda n: n.x)
    for node in queue:
        eliminateHole(node, outerNode)
        outerNode = filterPoints(outerNode, outerNode.next)
    return outerNode


def eliminateHole(hole, outerNode):
    outerNode = findHoleBridge(hole, outerNode)
    if outerNode:
        b = splitPolygon(outerNode, hole)
        filterPoints(b, b.next)


def findHoleBridge(hole, outerNode):
    do = True
    p = outerNode
    hx, hy = hole.x, hole.y
    qx = -math.inf
    m = None
    while do or p != outerNode:
        do = False
        if hy <= p.y and hy >= p.next.y and p.next.y - p.y != 0:
            x = p.x + (hy - p.y) * (p.next.x - p.x) / (p.next.y - p.y)
            if x <= hx and x > qx:
                qx = x
                if x == hx:
                    if hy == p.y:    return p
                    if hy == p.next.y: return p.next
                m = p if p.x < p.next.x else p.next
        p = p.next
    if not m:
        return None
    if hx == qx:
        return m.prev
    stop, mx, my = m, m.x, m.y
    tanMin = math.inf
    p = m.next
    while p != stop:
        hx_or_qx = hx if hy < my else qx
        qx_or_hx = qx if hy < my else hx
        if hx >= p.x >= mx and pointInTriangle(hx_or_qx, hy, mx, my, qx_or_hx, hy, p.x, p.y):
            tan = abs(hy - p.y) / (hx - p.x)
            if (tan < tanMin or (tan == tanMin and p.x > m.x)) and locallyInside(p, hole):
                m, tanMin = p, tan
        p = p.next
    return m


def indexCurve(start, minX, minY, size):
    do = True
    p = start
    while do or p != start:
        do = False
        if p.z is None:
            p.z = zOrder(p.x, p.y, minX, minY, size)
        p.prevZ = p.prev
        p.nextZ = p.next
        p = p.next
    p.prevZ.nextZ = None
    p.prevZ = None
    sortLinked(p)


def sortLinked(lst):
    do = True
    inSize = 1
    while do or numMerges > 1:
        do = False
        p = lst
        lst = tail = None
        numMerges = 0
        while p:
            numMerges += 1
            q, pSize = p, 0
            for _ in range(inSize):
                pSize += 1
                q = q.nextZ
                if not q: break
            qSize = inSize
            while pSize > 0 or (qSize > 0 and q):
                if pSize == 0:
                    e = q; q = q.nextZ; qSize -= 1
                elif qSize == 0 or not q:
                    e = p; p = p.nextZ; pSize -= 1
                elif p.z <= q.z:
                    e = p; p = p.nextZ; pSize -= 1
                else:
                    e = q; q = q.nextZ; qSize -= 1
                if tail: tail.nextZ = e
                else:    lst = e
                e.prevZ = tail
                tail = e
            p = q
        tail.nextZ = None
        inSize *= 2
    return lst


def zOrder(x, y, minX, minY, size):
    x = 32767 * int((x - minX) // size)
    y = 32767 * int((y - minY) // size)
    x = (x | (x << 8)) & 0x00FF00FF
    x = (x | (x << 4)) & 0x0F0F0F0F
    x = (x | (x << 2)) & 0x33333333
    x = (x | (x << 1)) & 0x55555555
    y = (y | (y << 8)) & 0x00FF00FF
    y = (y | (y << 4)) & 0x0F0F0F0F
    y = (y | (y << 2)) & 0x33333333
    y = (y | (y << 1)) & 0x55555555
    return x | (y << 1)


def getLeftmost(start):
    do = True
    p = leftmost = start
    while do or p != start:
        do = False
        if p.x < leftmost.x: leftmost = p
        p = p.next
    return leftmost


def pointInTriangle(ax, ay, bx, by, cx, cy, px, py):
    return ((cx-px)*(ay-py) - (ax-px)*(cy-py) >= 0 and
            (ax-px)*(by-py) - (bx-px)*(ay-py) >= 0 and
            (bx-px)*(cy-py) - (cx-px)*(by-py) >= 0)


def isValidDiagonal(a, b):
    return (a.next.i != b.i and a.prev.i != b.i and
            not intersectsPolygon(a, b) and
            locallyInside(a, b) and locallyInside(b, a) and middleInside(a, b))


def area(p, q, r):
    return (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y)


def equals(p1, p2):
    return p1.x == p2.x and p1.y == p2.y


def intersects(p1, q1, p2, q2):
    if (equals(p1, q1) and equals(p2, q2)) or (equals(p1, q2) and equals(p2, q1)):
        return True
    return (area(p1, q1, p2) > 0) != (area(p1, q1, q2) > 0) and \
           (area(p2, q2, p1) > 0) != (area(p2, q2, q1) > 0)


def intersectsPolygon(a, b):
    do = True
    p = a
    while do or p != a:
        do = False
        if (p.i != a.i and p.next.i != a.i and p.i != b.i and p.next.i != b.i
                and intersects(p, p.next, a, b)):
            return True
        p = p.next
    return False


def locallyInside(a, b):
    if area(a.prev, a, a.next) < 0:
        return area(a, b, a.next) >= 0 and area(a, a.prev, b) >= 0
    return area(a, b, a.prev) < 0 or area(a, a.next, b) < 0


def middleInside(a, b):
    do = True
    p = a
    inside = False
    px, py = (a.x + b.x) / 2, (a.y + b.y) / 2
    while do or p != a:
        do = False
        if ((p.y > py) != (p.next.y > py) and
                px < (p.next.x - p.x) * (py - p.y) / (p.next.y - p.y) + p.x):
            inside = not inside
        p = p.next
    return inside


def splitPolygon(a, b):
    a2, b2 = Node(a.i, a.x, a.y), Node(b.i, b.x, b.y)
    an, bp = a.next, b.prev
    a.next = b;  b.prev = a
    a2.next = an; an.prev = a2
    b2.next = a2; a2.prev = b2
    bp.next = b2; b2.prev = bp
    return b2


def insertNode(i, x, y, last):
    p = Node(i, x, y)
    if not last:
        p.prev = p.next = p
    else:
        p.next = last.next
        p.prev = last
        last.next.prev = p
        last.next = p
    return p


def removeNode(p):
    p.next.prev = p.prev
    p.prev.next = p.next
    if p.prevZ: p.prevZ.nextZ = p.nextZ
    if p.nextZ: p.nextZ.prevZ = p.prevZ


def signedArea(data, start, end, dim):
    s = 0
    j = end - dim
    for i in range(start, end, dim):
        s += (data[j] - data[i]) * (data[i + 1] + data[j + 1])
        j = i
    return s


class Node:
    __slots__ = ('i', 'x', 'y', 'prev', 'next', 'z', 'prevZ', 'nextZ', 'steiner')

    def __init__(self, i, x, y):
        self.i = i
        self.x = x
        self.y = y
        self.prev = self.next = None
        self.z = None
        self.prevZ = self.nextZ = None
        self.steiner = False
