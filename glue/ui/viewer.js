/**
 * viewer.js — Three.js 3D вьюер для Eagle футпринтов и STEP-моделей
 *
 * Экспортирует глобальный объект Viewer с методами:
 *   init(canvas)
 *   loadFootprint(geometry)
 *   loadComponentGLB(base64)
 *   setTransformMode(mode)   // 'translate' | 'rotate'
 *   resetCamera()
 *   resetComponent()
 *   getComponentTransform()  // → {tx,ty,tz,rx,ry,rz}
 *   setComponentTransform(tx,ty,tz,rx,ry,rz)
 *   onTransformChange(cb)    // callback при изменении трансформации
 */

const Viewer = (() => {
  // ── Internal state ──────────────────────────────────────────────────────
  let renderer, scene, camera, controls;
  let footprintGroup, componentGroup;
  let transformControls;
  let animFrameId;
  let onChangeCb = null;

  const COPPER_COLOR  = 0xC0C0C0;
  const COPPER_H      = 0.001;   // мм — тонкий чтобы компоненты лежали на плате
  const SILK_COLOR    = 0xFFFFFF;
  const BOARD_COLOR   = 0x1a7a2a;
  const BOARD_H       = 1.0;     // толщина платы в мм

  // ── Init ────────────────────────────────────────────────────────────────
  function init(canvas) {
    // Renderer
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setClearColor(0x111111);

    // Scene
    scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(5, 10, 8);
    scene.add(dir);
    const dir2 = new THREE.DirectionalLight(0xffffff, 0.3);
    dir2.position.set(-5, -5, -8);
    scene.add(dir2);

    // Grid
    const grid = new THREE.GridHelper(40, 40, 0x333333, 0x2a2a2a);
    grid.rotation.x = Math.PI / 2;  // XY плоскость
    scene.add(grid);

    // Orthographic camera
    const aspect = canvas.clientWidth / canvas.clientHeight;
    const frustum = 30;
    camera = new THREE.OrthographicCamera(
      -frustum * aspect, frustum * aspect,
       frustum, -frustum, 0.01, 1000
    );
    camera.position.set(0, -30, 30);
    camera.lookAt(0, 0, 0);

    // Groups
    footprintGroup  = new THREE.Group();
    componentGroup  = new THREE.Group();
    scene.add(footprintGroup);
    scene.add(componentGroup);

    // Orbit controls (inline, no addon needed for r128)
    controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;

    // Transform controls — built inline (gizmo)
    transformControls = new TransformGizmo(scene, camera, renderer, componentGroup);
    transformControls.onChange(() => {
      if (onChangeCb) onChangeCb(getComponentTransform());
    });

    // Keyboard shortcuts
    window.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT") return;
      if (e.key === "w" || e.key === "W") setTransformMode("translate");
      if (e.key === "e" || e.key === "E") setTransformMode("rotate");
    });

    // Resize
    const ro = new ResizeObserver(() => onResize(canvas));
    ro.observe(canvas.parentElement);
    onResize(canvas);

    // Render loop
    (function animate() {
      animFrameId = requestAnimationFrame(animate);
      controls.update();
      transformControls.update();
      renderer.render(scene, camera);
    })();
  }

  function onResize(canvas) {
    const w = canvas.parentElement.clientWidth;
    const h = canvas.parentElement.clientHeight;
    renderer.setSize(w, h, false);
    if (controls && controls._apply) controls._apply();
  }

  // ── Footprint geometry ──────────────────────────────────────────────────
  function loadFootprint(geom) {
    // Очищаем
    while (footprintGroup.children.length) {
      footprintGroup.remove(footprintGroup.children[0]);
    }

    const COPPER_LAYERS = new Set(["1","16","18"]);
    const SILK_LAYERS   = new Set(["21","22"]);

    // ── Подложка (bounding box по падам + проводникам) ──
    const allX = [], allY = [];
    const collect = pts => { pts.forEach(([x,y]) => { allX.push(x); allY.push(y); }); };

    (geom.pads || []).forEach(p => {
      const r = p.drill / 2 + 0.5;
      collect([[p.x-r,p.y-r],[p.x+r,p.y+r]]);
    });
    (geom.smds || []).forEach(s => {
      collect([[s.x-s.dx/2,s.y-s.dy/2],[s.x+s.dx/2,s.y+s.dy/2]]);
    });
    (geom.wires || []).forEach(w => {
      collect([[w.x1,w.y1],[w.x2,w.y2]]);
    });

    if (allX.length) {
      const margin = 1.0;
      const bw = (Math.max(...allX) - Math.min(...allX)) + 2*margin;
      const bh = (Math.max(...allY) - Math.min(...allY)) + 2*margin;
      const cx = (Math.max(...allX) + Math.min(...allX)) / 2;
      const cy = (Math.max(...allY) + Math.min(...allY)) / 2;

      // Board shape with drill holes cut out
      const boardShape = new THREE.Shape();
      boardShape.moveTo(cx - bw/2, cy - bh/2);
      boardShape.lineTo(cx + bw/2, cy - bh/2);
      boardShape.lineTo(cx + bw/2, cy + bh/2);
      boardShape.lineTo(cx - bw/2, cy + bh/2);
      boardShape.closePath();

      // Cut drill holes into board shape
      (geom.pads || []).forEach(p => {
        const drillR = p.drill / 2;
        const holePath = new THREE.Path();
        holePath.absarc(p.x, p.y, drillR, 0, Math.PI * 2, true);
        boardShape.holes.push(holePath);
      });

      const boardGeo = new THREE.ExtrudeGeometry(boardShape, {
        depth: BOARD_H, bevelEnabled: false
      });
      const boardMat = new THREE.MeshLambertMaterial({ color: BOARD_COLOR });
      const board = new THREE.Mesh(boardGeo, boardMat);
      board.position.set(0, 0, -BOARD_H);
      footprintGroup.add(board);
    }

    // ── Пады сквозного монтажа — кольцевые площадки сверху и снизу ──
    const copperMat = new THREE.MeshLambertMaterial({ color: COPPER_COLOR });
    (geom.pads || []).forEach(p => {
      const outerR = p.drill / 2 + 0.5;
      const drillR = p.drill / 2;

      function makeRing() {
        const shape = new THREE.Shape();
        shape.absarc(0, 0, outerR, 0, Math.PI*2, false);
        const h = new THREE.Path();
        h.absarc(0, 0, drillR, 0, Math.PI*2, true);
        shape.holes.push(h);
        return new THREE.ExtrudeGeometry(shape, { depth: COPPER_H, bevelEnabled: false });
      }

      // Верхняя площадка на Z=0
      const topRing = new THREE.Mesh(makeRing(), copperMat);
      topRing.position.set(p.x, p.y, 0);
      footprintGroup.add(topRing);

      // Нижняя площадка на обратной стороне платы
      const botRing = new THREE.Mesh(makeRing(), copperMat);
      botRing.position.set(p.x, p.y, -BOARD_H - COPPER_H);
      footprintGroup.add(botRing);
    });

    // ── SMD пады ──
    (geom.smds || []).forEach(s => {
      const shape = new THREE.Shape();
      shape.moveTo(-s.dx/2, -s.dy/2);
      shape.lineTo( s.dx/2, -s.dy/2);
      shape.lineTo( s.dx/2,  s.dy/2);
      shape.lineTo(-s.dx/2,  s.dy/2);
      shape.closePath();
      const extGeo = new THREE.ExtrudeGeometry(shape, {
        depth: COPPER_H, bevelEnabled: false
      });
      const mesh = new THREE.Mesh(extGeo, copperMat);
      mesh.position.set(s.x, s.y, 0);
      mesh.rotation.z = s.rot * Math.PI / 180;
      footprintGroup.add(mesh);
    });

    // ── Проводники ──
    // Для шелкографии (layer 21) используем ширину из w.width через TubeGeometry
    function makeTubePath(points3d) {
      return {
        getPoints: () => points3d,
        getSpacedPoints: (n) => {
          const pts = [];
          for (let i = 0; i <= n; i++) {
            const t = i / n;
            const idx = Math.min(Math.floor(t * (points3d.length-1)), points3d.length-2);
            const a = points3d[idx], b = points3d[idx+1];
            const f = t * (points3d.length-1) - idx;
            pts.push(new THREE.Vector3(
              a.x + (b.x-a.x)*f, a.y + (b.y-a.y)*f, a.z + (b.z-a.z)*f));
          }
          return pts;
        }
      };
    }

    (geom.wires || []).forEach(w => {
      const isSilk   = SILK_LAYERS.has(w.layer);
      const isCopper = COPPER_LAYERS.has(w.layer);
      if (!isSilk && !isCopper) return;

      const color = isSilk ? SILK_COLOR : COPPER_COLOR;
      const zBase = isCopper ? COPPER_H : 0.01;
      // Use actual wire width for silk; fallback to thin line for copper
      const wireWidth = (isSilk && w.width > 0) ? w.width : 0;

      let pts3d;
      if (Math.abs(w.curve) < 0.01) {
        pts3d = [
          new THREE.Vector3(w.x1, w.y1, zBase),
          new THREE.Vector3(w.x2, w.y2, zBase),
        ];
      } else {
        const arcPts = arcToPoints(w.x1, w.y1, w.x2, w.y2, w.curve, 32);
        pts3d = arcPts.map(([x,y]) => new THREE.Vector3(x, y, zBase));
      }

      if (wireWidth > 0) {
        // Silk with real width: use flat rounded rectangle extrusion along path
        // Simple approach: extrude a small rect shape along the path segments
        const r = wireWidth / 2;
        const mat3d = new THREE.MeshLambertMaterial({ color });
        for (let i = 0; i < pts3d.length - 1; i++) {
          const a = pts3d[i], b = pts3d[i+1];
          const dx = b.x-a.x, dy = b.y-a.y;
          const len = Math.sqrt(dx*dx+dy*dy);
          if (len < 1e-6) continue;
          // Box oriented along segment
          const geo = new THREE.BoxGeometry(len, wireWidth, COPPER_H);
          const mesh = new THREE.Mesh(geo, mat3d);
          mesh.position.set((a.x+b.x)/2, (a.y+b.y)/2, zBase + COPPER_H/2);
          mesh.rotation.z = Math.atan2(dy, dx);
          footprintGroup.add(mesh);
        }
        return;
      }

      // Thin line fallback (copper wires)
      {
        const geo  = new THREE.BufferGeometry().setFromPoints(pts3d);
        const mat  = new THREE.LineBasicMaterial({ color });
        footprintGroup.add(new THREE.Line(geo, mat));
      }
    });

    // ── Окружности ──
    (geom.circles || []).forEach(c => {
      const isSilk = SILK_LAYERS.has(c.layer);
      const color  = isSilk ? SILK_COLOR : COPPER_COLOR;
      const zBase  = isSilk ? 0.01 : COPPER_H;
      const pts = [];
      for (let i = 0; i <= 64; i++) {
        const a = (i / 64) * Math.PI * 2;
        pts.push(new THREE.Vector3(
          c.x + c.radius * Math.cos(a),
          c.y + c.radius * Math.sin(a),
          zBase
        ));
      }
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const mat = new THREE.LineBasicMaterial({ color });
      footprintGroup.add(new THREE.Line(geo, mat));
    });
  }

  // ── Arc helper ──────────────────────────────────────────────────────────
  function arcToPoints(x1,y1,x2,y2,curve,n) {
    const dx=x2-x1, dy=y2-y1;
    const chord = Math.hypot(dx,dy);
    if (chord < 1e-10) return [[x1,y1],[x2,y2]];
    const alpha = Math.abs(curve)/2 * Math.PI/180;
    const r = chord / (2*Math.sin(alpha));
    const mx=(x1+x2)/2, my=(y1+y2)/2;
    const d = Math.sqrt(Math.max(r*r-(chord/2)**2,0));
    const px=-dy/chord, py=dx/chord;
    const sign = curve>0 ? 1 : -1;
    const cx=mx+sign*d*px, cy=my+sign*d*py;
    let a1=Math.atan2(y1-cy,x1-cx), a2=Math.atan2(y2-cy,x2-cx);
    if (curve>0) { if (a2<a1) a2+=2*Math.PI; }
    else          { if (a2>a1) a2-=2*Math.PI; }
    const pts=[];
    for (let i=0;i<=n;i++) {
      const a=a1+(a2-a1)*i/n;
      pts.push([cx+r*Math.cos(a), cy+r*Math.sin(a)]);
    }
    return pts;
  }

  // ── Component GLB ────────────────────────────────────────────────────────
  function loadComponentGLB(base64str) {
    while (componentGroup.children.length) {
      componentGroup.remove(componentGroup.children[0]);
    }
    if (!base64str) { transformControls.detach(); return Promise.resolve(); }

    const binary = atob(base64str);
    const buffer = new ArrayBuffer(binary.length);
    const bytes  = new Uint8Array(buffer);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

    return parseGLB(buffer).then(({group, info}) => {
      group.scale.setScalar(1000); // OCCT WriteGltf пишет в метрах, viewer — в мм
      componentGroup.add(group);
      transformControls.attach(componentGroup);
      return info;
    });
  }

  // GLB парсер — обходит всё дерево nodes/meshes сцены
  async function parseGLB(buffer) {
    const dv        = new DataView(buffer);
    const jsonLen   = dv.getUint32(12, true);
    const jsonStr   = new TextDecoder().decode(new Uint8Array(buffer, 20, jsonLen));
    const gltf      = JSON.parse(jsonStr);
    const binOffset = 20 + jsonLen + 8;

    let primCount = 0;
    let bbox = { minX:Infinity, maxX:-Infinity, minY:Infinity, maxY:-Infinity, minZ:Infinity, maxZ:-Infinity };

    function getComponentCount(type) {
      return {SCALAR:1, VEC2:2, VEC3:3, VEC4:4, MAT4:16}[type] ?? 1;
    }

    // buffer.slice() гарантирует правильное выравнивание для TypedArray
    function getArr(accIdx, TypedArray) {
      const acc   = gltf.accessors[accIdx];
      const bv    = gltf.bufferViews[acc.bufferView];
      const start = binOffset + (bv.byteOffset ?? 0) + (acc.byteOffset ?? 0);
      const comp  = getComponentCount(acc.type);
      const bytes = acc.count * comp * TypedArray.BYTES_PER_ELEMENT;
      return new TypedArray(buffer.slice(start, start + bytes));
    }

    function buildPrimitive(primitive) {
      const geo = new THREE.BufferGeometry();

      const posArr = getArr(primitive.attributes.POSITION, Float32Array);
      geo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));

      // Обновляем bbox для диагностики
      for (let i = 0; i < posArr.length; i += 3) {
        bbox.minX = Math.min(bbox.minX, posArr[i]);   bbox.maxX = Math.max(bbox.maxX, posArr[i]);
        bbox.minY = Math.min(bbox.minY, posArr[i+1]); bbox.maxY = Math.max(bbox.maxY, posArr[i+1]);
        bbox.minZ = Math.min(bbox.minZ, posArr[i+2]); bbox.maxZ = Math.max(bbox.maxZ, posArr[i+2]);
      }

      if (primitive.attributes.NORMAL !== undefined) {
        geo.setAttribute("normal",
          new THREE.BufferAttribute(getArr(primitive.attributes.NORMAL, Float32Array), 3));
      }

      if (primitive.indices !== undefined) {
        const idxAcc   = gltf.accessors[primitive.indices];
        const TypedIdx = idxAcc.componentType === 5125 ? Uint32Array : Uint16Array;
        geo.setIndex(new THREE.BufferAttribute(getArr(primitive.indices, TypedIdx), 1));
      }

      if (!geo.attributes.normal) geo.computeVertexNormals();

      const mat       = gltf.materials?.[primitive.material];
      const cf        = mat?.pbrMetallicRoughness?.baseColorFactor ?? [0.7, 0.7, 0.75, 1.0];
      const metallic  = mat?.pbrMetallicRoughness?.metallicFactor  ?? 0.2;
      const roughness = mat?.pbrMetallicRoughness?.roughnessFactor ?? 0.6;

      primCount++;
      return new THREE.Mesh(geo, new THREE.MeshPhongMaterial({
        color:     new THREE.Color(cf[0], cf[1], cf[2]),
        shininess: Math.round((1.0 - roughness) * 80) + 10,
        specular:  new THREE.Color(metallic * 0.5, metallic * 0.5, metallic * 0.5),
      }));
    }

    function visitNode(nodeIdx, parent) {
      const node = gltf.nodes[nodeIdx];
      const obj  = new THREE.Group();

      if (node.matrix) {
        new THREE.Matrix4().fromArray(node.matrix)
          .decompose(obj.position, obj.quaternion, obj.scale);
      } else {
        if (node.translation) obj.position.fromArray(node.translation);
        if (node.rotation)    obj.quaternion.fromArray(node.rotation);
        if (node.scale)       obj.scale.fromArray(node.scale);
      }

      if (node.mesh !== undefined) {
        for (const prim of (gltf.meshes[node.mesh].primitives ?? [])) {
          obj.add(buildPrimitive(prim));
        }
      }

      for (const childIdx of (node.children ?? [])) {
        visitNode(childIdx, obj);
      }

      parent.add(obj);
    }

    const group      = new THREE.Group();
    const sceneIdx   = gltf.scene ?? 0;
    const sceneNodes = gltf.scenes?.[sceneIdx]?.nodes ?? [];

    if (sceneNodes.length > 0) {
      for (const nodeIdx of sceneNodes) {
        visitNode(nodeIdx, group);
      }
    } else {
      // fallback: читаем meshes[0] напрямую
      for (const prim of (gltf.meshes?.[0]?.primitives ?? [])) {
        group.add(buildPrimitive(prim));
      }
    }

    const r = v => v.toFixed(2);
    const info = primCount === 0
      ? `0 primitives! nodes=${gltf.nodes?.length ?? 0} meshes=${gltf.meshes?.length ?? 0} sceneNodes=${sceneNodes.length}`
      : `${primCount} prim, X[${r(bbox.minX)},${r(bbox.maxX)}] Y[${r(bbox.minY)},${r(bbox.maxY)}] Z[${r(bbox.minZ)},${r(bbox.maxZ)}]`;

    return { group, info };
  }

  // ── Transform ────────────────────────────────────────────────────────────
  function setTransformMode(mode) {
    transformControls.setMode(mode);
    document.getElementById("btn-translate").classList.toggle("active", mode==="translate");
    document.getElementById("btn-rotate").classList.toggle("active", mode==="rotate");
  }



  function resetComponent() {
    componentGroup.position.set(0, 0, 0);
    componentGroup.rotation.set(0, 0, 0);
    if (onChangeCb) onChangeCb(getComponentTransform());
  }

  function getComponentTransform() {
    const p = componentGroup.position;
    const r = componentGroup.rotation;
    const toDeg = v => parseFloat((v * 180 / Math.PI).toFixed(4));
    return {
      tx: parseFloat(p.x.toFixed(4)),
      ty: parseFloat(p.y.toFixed(4)),
      tz: parseFloat(p.z.toFixed(4)),
      rx: toDeg(r.x),
      ry: toDeg(r.y),
      rz: toDeg(r.z),
    };
  }

  function setComponentTransform(tx,ty,tz,rx,ry,rz) {
    componentGroup.position.set(+tx, +ty, +tz);
    componentGroup.rotation.set(
      rx * Math.PI / 180,
      ry * Math.PI / 180,
      rz * Math.PI / 180
    );
  }

  function onTransformChange(cb) { onChangeCb = cb; }

  function setCameraPreset(preset) {
    if (controls && controls.setPreset) controls.setPreset(preset);
    if (transformControls && transformControls.setPreset) transformControls.setPreset(preset);
  }

  function resetCamera() {
    if (controls && controls.reset) controls.reset();
    if (transformControls && transformControls.setPreset) transformControls.setPreset(null);
  }

  function autoPlace() {
    if (componentGroup.children.length === 0) return false;

    // Compute AABB of the component in world space
    const box = new THREE.Box3().setFromObject(componentGroup);
    if (box.isEmpty()) return false;

    const center = new THREE.Vector3();
    box.getCenter(center);

    // XY: move so that component center XY aligns with footprint center (0,0)
    // Z:  move so that component bottom (box.min.z) touches Z=0 (footprint surface)
    const newX = componentGroup.position.x - center.x;
    const newY = componentGroup.position.y - center.y;
    const newZ = componentGroup.position.z - box.min.z;

    componentGroup.position.set(newX, newY, newZ);

    if (onChangeCb) onChangeCb(getComponentTransform());
    return true;
  }

  return {
    init, loadFootprint, loadComponentGLB,
    setTransformMode, resetCamera, resetComponent,
    getComponentTransform, setComponentTransform, onTransformChange,
    setCameraPreset, autoPlace,
  };
})();


// ══════════════════════════════════════════════════════════════════════════
//  Shared gizmo drag flag
// ══════════════════════════════════════════════════════════════════════════
let _gizmoDragging = false;

// ══════════════════════════════════════════════════════════════════════════
//  OrbitControls — Fusion360-style camera
//    LMB       — orbit (rotate)
//    MMB drag  — pan (shift target)
//    Wheel     — zoom
// ══════════════════════════════════════════════════════════════════════════
function OrbitControls(camera, domEl) {
  // Spherical coords around target
  let theta = -Math.PI * 0.25;   // horizontal angle — isometric-ish start
  let phi   =  Math.PI * 0.35;   // vertical angle   — "table" view
  let dist  = 30;                 // camera distance (not used for ortho zoom)
  let zoom  = 3.5;                // ortho frustum scale — start zoomed in

  // Pan offset (shifts the lookAt target)
  let panX = 0, panY = 0;

  let orbitDown = false, panDown = false;
  let lastX = 0, lastY = 0;

  // ── Apply camera ──────────────────────────────────────────────────────
  const apply = () => {
    // Spherical → Cartesian (Z-up: phi from Z-axis)
    const sinPhi = Math.sin(phi), cosPhi = Math.cos(phi);
    camera.position.set(
      panX + dist * sinPhi * Math.sin(theta),
      panY - dist * sinPhi * Math.cos(theta),
      dist * cosPhi
    );
    camera.up.set(0, 0, 1);
    camera.lookAt(panX, panY, 0);

    const w = domEl.clientWidth  || 800;
    const h = domEl.clientHeight || 600;
    const aspect   = w / h;
    const halfSize = 15 / zoom;    // base frustum half-size = 15
    camera.left   = -halfSize * aspect;
    camera.right  =  halfSize * aspect;
    camera.top    =  halfSize;
    camera.bottom = -halfSize;
    camera.updateProjectionMatrix();
  };

  apply();

  // ── Mouse events ──────────────────────────────────────────────────────
  domEl.addEventListener("contextmenu", e => e.preventDefault());

  domEl.addEventListener("mousedown", e => {
    if (e.button === 0) {                       // LMB — orbit
      orbitDown = true; lastX = e.clientX; lastY = e.clientY;
    } else if (e.button === 1) {                // MMB — pan
      panDown = true; lastX = e.clientX; lastY = e.clientY;
      e.preventDefault();
    }
  });

  window.addEventListener("mouseup", e => {
    if (e.button === 0) orbitDown = false;
    if (e.button === 1) panDown   = false;
  });

  window.addEventListener("mousemove", e => {
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;

    if (orbitDown && !_gizmoDragging) {
      // Fusion360: LMB drag — orbit, no clamping (full rollover)
      theta -= dx * 0.005;
      phi   -= dy * 0.005;
      // Rollover: wrap phi so camera flips naturally
      if (phi < 0.001)        phi = 0.001;        // avoid gimbal at pole
      if (phi > Math.PI-0.001) phi = Math.PI-0.001;
      apply();
    }

    if (panDown) {
      // Pan in screen space: move target perpendicular to view direction
      const w = domEl.clientWidth  || 800;
      const h = domEl.clientHeight || 600;
      const halfSize = 15 / zoom;
      // pixels → world units
      const scaleX = (halfSize * 2 * (w / h)) / w;
      const scaleY = (halfSize * 2) / h;

      // Right vector in XY plane (perpendicular to view, horizontal)
      const rightX =  Math.cos(theta);
      const rightY =  Math.sin(theta);
      // Up vector in XY plane (perpendicular to view, vertical component in XY)
      const upX = -Math.sin(phi) * Math.sin(theta) * 0 - Math.cos(phi) * Math.sin(theta);
      const upY =  Math.cos(phi) * Math.cos(theta);

      panX -= dx * scaleX * rightX - dy * scaleY * upX;
      panY -= dx * scaleX * rightY - dy * scaleY * upY;
      apply();
    }
  });

  domEl.addEventListener("wheel", e => {
    const factor = e.deltaY > 0 ? 0.88 : 1.12;
    zoom = Math.max(0.05, Math.min(100, zoom * factor));
    apply();
    e.preventDefault();
  }, { passive: false });

  // ── Public API ────────────────────────────────────────────────────────
  this.update = () => {};

  this.reset = () => {
    theta = -Math.PI * 0.25; phi = Math.PI * 0.35;
    dist = 30; zoom = 3.5; panX = 0; panY = 0;
    apply();
  };

  this.setPreset = (preset) => {
    panX = 0; panY = 0; zoom = 3.5; dist = 30;
    switch (preset) {
      case "top":    phi = 0.001;           theta = 0;           break;
      case "bottom": phi = Math.PI - 0.001; theta = 0;           break;
      case "left":   phi = Math.PI / 2;     theta = -Math.PI/2;  break;
      case "right":  phi = Math.PI / 2;     theta =  Math.PI/2;  break;
      case "front":  phi = Math.PI / 2;     theta = 0;           break;
    }
    apply();
  };

  // Expose apply for resize
  this._apply = apply;
}


// ══════════════════════════════════════════════════════════════════════════
//  TransformGizmo
// ══════════════════════════════════════════════════════════════════════════
function TransformGizmo(scene, camera, renderer, target) {
  let mode     = "translate";
  let changeCb = null;
  let isDragging    = false;
  let isFreeDrag    = false;   // drag on component body (2-axis)
  let freeDragPlane = "xy";    // which plane for free drag
  let dragAxis   = null;
  let hoveredAxis = null;
  let startMouse = new THREE.Vector2();
  let startPos   = new THREE.Vector3();
  let startRot   = new THREE.Euler();

  // Current camera preset (set from outside)
  let _currentPreset = null;

  const COLORS     = { x: 0xff3333, y: 0x33ff33, z: 0x3399ff };
  const COLORS_HOV = { x: 0xff9999, y: 0x99ff99, z: 0x99ccff };
  const gizmoGroup = new THREE.Group();
  scene.add(gizmoGroup);

  // Store material refs for hover update
  const axisMaterials = {};   // ax → [material, ...]

  function buildGizmo() {
    while (gizmoGroup.children.length) gizmoGroup.remove(gizmoGroup.children[0]);
    Object.keys(axisMaterials).forEach(k => delete axisMaterials[k]);
    if (!target) return;

    const len = 6;   // arrow length in world units
    for (const ax of ["x","y","z"]) {
      const col = COLORS[ax];
      const mats = [];

      if (mode === "translate") {
        // Shaft — thick cylinder for easier picking
        const shaftGeo = new THREE.CylinderGeometry(0.084, 0.084, len, 8);
        const shaftMat = new THREE.MeshBasicMaterial({ color: col });
        const shaft    = new THREE.Mesh(shaftGeo, shaftMat);
        // Position shaft along axis
        if (ax==="x") { shaft.rotation.z = -Math.PI/2; shaft.position.x = len/2; }
        if (ax==="y") { shaft.position.y = len/2; }
        if (ax==="z") { shaft.rotation.x =  Math.PI/2; shaft.position.z = len/2; }
        shaft.userData.axis = ax;
        gizmoGroup.add(shaft);
        mats.push(shaftMat);

        // Arrowhead cone
        const coneGeo = new THREE.ConeGeometry(0.245, 1.0, 12);
        const coneMat = new THREE.MeshBasicMaterial({ color: col });
        const cone    = new THREE.Mesh(coneGeo, coneMat);
        if (ax==="x") { cone.rotation.z = -Math.PI/2; cone.position.x = len + 0.5; }
        if (ax==="y") {                                cone.position.y = len + 0.5; }
        if (ax==="z") { cone.rotation.x =  Math.PI/2; cone.position.z = len + 0.5; }
        cone.userData.axis = ax;
        gizmoGroup.add(cone);
        mats.push(coneMat);

      } else {
        // Rotation ring — thicker tube
        const ringGeo = new THREE.TorusGeometry(len * 0.6, 0.126, 8, 48);
        const ringMat = new THREE.MeshBasicMaterial({ color: col });
        const ring    = new THREE.Mesh(ringGeo, ringMat);
        // TorusGeometry default: lies in XY plane (rotates around Z)
        // red  (X): rotate around X → ring in YZ plane → rotate ring by Y=π/2
        // green(Y): rotate around Z → ring in XY plane → no rotation (default)
        // blue (Z): rotate around Y → ring in XZ plane → rotate ring by X=π/2
        if (ax==="x") ring.rotation.y = Math.PI/2;
        if (ax==="z") ring.rotation.x = Math.PI/2;
        // ax==="y": no rotation needed
        ring.userData.axis = ax;
        gizmoGroup.add(ring);
        mats.push(ringMat);
      }

      axisMaterials[ax] = mats;
    }

    // Pivot indicator: red dot at rotation center (rotate mode only)
    const oldPivot = gizmoGroup.getObjectByName("pivot_dot");
    if (oldPivot) gizmoGroup.remove(oldPivot);
    if (mode === "rotate") {
      const pivotGeo = new THREE.SphereGeometry(0.25, 12, 12);
      const pivotMat = new THREE.MeshBasicMaterial({ color: 0xff0000 });
      const pivotDot = new THREE.Mesh(pivotGeo, pivotMat);
      pivotDot.name = "pivot_dot";
      pivotDot.userData.axis = null;  // not pickable as axis
      gizmoGroup.add(pivotDot);
    }
  }

  function setHover(ax) {
    if (ax === hoveredAxis) return;
    hoveredAxis = ax;
    for (const [a, mats] of Object.entries(axisMaterials)) {
      const col = a === ax ? COLORS_HOV[a] : COLORS[a];
      mats.forEach(m => m.color.setHex(col));
    }
  }

  this.attach  = () => { buildGizmo(); };
  this.detach  = () => { while (gizmoGroup.children.length) gizmoGroup.remove(gizmoGroup.children[0]); };
  this.setMode = (m)  => { mode = m; buildGizmo(); };
  this.onChange = (cb) => { changeCb = cb; };
  this.setPreset = (p) => { _currentPreset = p; };
  this.update    = () => {
    if (target) gizmoGroup.position.copy(target.position);
  };

  const canvas    = renderer.domElement;
  const raycaster = new THREE.Raycaster();
  const mouse2    = new THREE.Vector2();

  function getNDC(e) {
    const rect = canvas.getBoundingClientRect();
    return new THREE.Vector2(
       ((e.clientX - rect.left) / rect.width)  * 2 - 1,
      -((e.clientY - rect.top)  / rect.height) * 2 + 1
    );
  }

  function pxToWorld() {
    const w = canvas.clientWidth  || 800;
    const h = canvas.clientHeight || 600;
    return {
      x: (camera.right - camera.left) / w,
      y: (camera.top - camera.bottom) / h,
    };
  }

  // Original component materials for hover restore
  const _origMaterials = new WeakMap();
  let _compHovered = false;

  function setCompHover(on) {
    if (on === _compHovered) return;
    _compHovered = on;
    target.traverse(obj => {
      if (!obj.isMesh) return;
      if (on) {
        // Store original and apply brightened clone
        if (!_origMaterials.has(obj)) {
          _origMaterials.set(obj, obj.material);
        }
        const m = obj.material.clone();
        m.color.multiplyScalar(1.5);
        obj.material = m;
      } else {
        const orig = _origMaterials.get(obj);
        if (orig) obj.material = orig;
      }
    });
  }

  // ── Hover detection ───────────────────────────────────────────────────
  canvas.addEventListener("mousemove", e => {
    if (isDragging || isFreeDrag) return;
    mouse2.copy(getNDC(e));
    raycaster.setFromCamera(mouse2, camera);

    // Gizmo hover
    const gizmoHits = raycaster.intersectObjects(gizmoGroup.children, true);
    setHover(gizmoHits.length > 0 ? gizmoHits[0].object.userData.axis : null);

    // Component body hover (only in translate mode + preset view)
    if (mode === "translate" && _currentPreset && target.children.length > 0
        && gizmoHits.length === 0) {
      const compHits = raycaster.intersectObjects(target.children, true);
      setCompHover(compHits.length > 0);
    } else {
      setCompHover(false);
    }
  });

  // ── Mouse down: gizmo (RMB) or free-drag on component (RMB) ──────────
  canvas.addEventListener("mousedown", e => {
    if (e.button !== 2) return;

    mouse2.copy(getNDC(e));
    raycaster.setFromCamera(mouse2, camera);

    // 1. Try gizmo first
    const gizmoHits = raycaster.intersectObjects(gizmoGroup.children, true);
    if (gizmoHits.length > 0) {
      dragAxis       = gizmoHits[0].object.userData.axis;
      isDragging     = true;
      _gizmoDragging = true;
      startMouse.set(e.clientX, e.clientY);
      startPos.copy(target.position);
      startRot.copy(target.rotation);
      e.preventDefault(); e.stopPropagation();
      return;
    }

    // 2. Free drag on component body (only in preset view, translate mode)
    if (mode === "translate" && _currentPreset && target.children.length > 0) {
      const compHits = raycaster.intersectObjects(target.children, true);
      if (compHits.length > 0) {
        isFreeDrag     = true;
        _gizmoDragging = true;
        freeDragPlane  = (_currentPreset === "left" || _currentPreset === "right")
                         ? "xz" : "xy";
        startMouse.set(e.clientX, e.clientY);
        startPos.copy(target.position);
        e.preventDefault(); e.stopPropagation();
      }
    }
  });

  // ── Mouse move: apply transform ───────────────────────────────────────
  window.addEventListener("mousemove", e => {
    const pw = pxToWorld();
    const screenDX = (e.clientX - startMouse.x) * pw.x;
    const screenDY = (e.clientY - startMouse.y) * pw.y;

    if (isDragging && dragAxis) {
      if (mode === "translate") {
        target.position.copy(startPos);
        // X: mouse right → component right (+X)
        // Y: mouse up → component up. Screen Y is inverted relative to world Y,
        //    AND green axis (Y) direction should match intuition → invert screenDY
        // Z: mouse up → component up (along Z)
        if (dragAxis === "x") target.position.x += screenDX;
        if (dragAxis === "y") target.position.y -= screenDY;   // screen Y inverted
        if (dragAxis === "z") target.position.z -= screenDY;
      } else {
        target.rotation.copy(startRot);
        const delta = Math.abs(screenDX) > Math.abs(screenDY) ? screenDX : -screenDY;
        const angle = delta * 0.15;
        if (dragAxis === "x") target.rotation.x = startRot.x + angle;
        if (dragAxis === "y") target.rotation.z = startRot.z - angle;   // green → Z, inverted
        if (dragAxis === "z") target.rotation.y = startRot.y + angle;   // blue  → Y
      }
      if (changeCb) changeCb();
    }

    if (isFreeDrag) {
      target.position.copy(startPos);
      if (freeDragPlane === "xy") {
        target.position.x += screenDX;
        target.position.y -= screenDY;   // screen Y inverted → world Y
      } else {  // xz
        target.position.x += screenDX;
        target.position.z -= screenDY;
      }
      if (changeCb) changeCb();
    }
  });

  // ── Mouse up ──────────────────────────────────────────────────────────
  window.addEventListener("mouseup", e => {
    if (e.button === 2) {
      isDragging = isFreeDrag = false;
      dragAxis = null;
      _gizmoDragging = false;
    }
  });

  // Clear hover when mouse leaves canvas
  canvas.addEventListener("mouseleave", () => {
    setHover(null);
    setCompHover(false);
  });
}