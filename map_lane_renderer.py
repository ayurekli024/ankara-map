import math
import os
import json
import time
import requests
import pygame

# --- AYARLAR ---
# Ankara 50x50 km (2500 km2)
BBOX = [39.6950, 32.5600, 40.1450, 33.1480] 
SCREEN_WIDTH = 1500
SCREEN_HEIGHT = 700
FPS = 60

# Haritayı kaç parçaya böleceğiz? (4x4 = 16 Parça)
GRID_SIZE = 4 
CACHE_DIR = "ankara_chunks"

# Klasör yoksa oluştur
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- 1. CHUNKING (PARÇALI İNDİRME) MOTORU ---
def fetch_osm_chunks(bbox, grid_size):
    min_lat, min_lon, max_lat, max_lon = bbox
    d_lat = (max_lat - min_lat) / grid_size
    d_lon = (max_lon - min_lon) / grid_size
    
    chunk_files = []
    session = requests.Session()
    headers = {"User-Agent": "OSMChunkDownloader/1.0", "Content-Type": "application/x-www-form-urlencoded"}
    
    total_chunks = grid_size * grid_size
    current_chunk = 1

    for i in range(grid_size):
        for j in range(grid_size):
            c_min_lat = min_lat + i * d_lat
            c_min_lon = min_lon + j * d_lon
            c_max_lat = min_lat + (i + 1) * d_lat
            c_max_lon = min_lon + (j + 1) * d_lon
            
            filename = os.path.join(CACHE_DIR, f"chunk_{i}_{j}.json")
            chunk_files.append(filename)
            
            # Eğer parça zaten indirilmişse atla
            if os.path.exists(filename):
                print(f"[CACHE] Parça {current_chunk}/{total_chunks} diskten okundu: {filename}")
                current_chunk += 1
                continue
                
            query = f"""[out:json][timeout:180];
            (
              way["highway"]({c_min_lat},{c_min_lon},{c_max_lat},{c_max_lon});
              way["building"]({c_min_lat},{c_min_lon},{c_max_lat},{c_max_lon});
            );
            out body;
            >;
            out body qt;""" # <-- skel yerine body yapıldı

            print(f"[AG] İndiriliyor: Parça {current_chunk}/{total_chunks}...")
            
            # Ban yememek için kararlı sunucu kullanımı
            url = "https://lz4.overpass-api.de/api/interpreter"
            success = False
            
            while not success:
                try:
                    resp = session.post(url, data={"data": query}, headers=headers, timeout=200)
                    if resp.status_code == 200:
                        with open(filename, "w", encoding="utf-8") as f:
                            json.dump(resp.json(), f)
                        success = True
                        print(f"[BAŞARILI] Kaydedildi. (Sunucuyu dinlendirmek için 10 sn bekleniyor...)")
                        time.sleep(10) # Overpass sunucusunu yormamak için zorunlu bekleme
                    elif resp.status_code == 429:
                        print("[UYARI] Çok fazla istek! 30 saniye bekleniyor...")
                        time.sleep(30)
                    else:
                        print(f"[HATA] HTTP {resp.status_code}. 5 sn sonra tekrar denenecek.")
                        time.sleep(5)
                except Exception as e:
                    print(f"[BAĞLANTI HATASI] {e}, 5 sn sonra tekrar denenecek...")
                    time.sleep(5)
                    
            current_chunk += 1
            
    return chunk_files

# --- PROJEKSİYON (Değişmedi) ---
class MapProjector:
    def __init__(self, bbox, width, height, padding=40):
        self.width = width
        self.height = height
        self.padding = padding
        self.min_lat, self.min_lon, self.max_lat, self.max_lon = bbox

        self.min_x = self.min_lon
        self.max_x = self.max_lon
        self.min_y = self._lat_to_merc(self.min_lat)
        self.max_y = self._lat_to_merc(self.max_lat)

        avg_lat = (self.min_lat + self.max_lat) / 2.0
        lon_degree_meters = math.cos(math.radians(avg_lat)) * 111320.0
        real_width_meters = (self.max_lon - self.min_lon) * lon_degree_meters
        
        draw_w = self.width - 2 * self.padding
        self.units_per_meter = draw_w / real_width_meters

    def _lat_to_merc(self, lat):
        rad = math.radians(lat)
        return math.log(math.tan(math.pi / 4 + rad / 2))

    def project(self, lat, lon):
        y_merc = self._lat_to_merc(lat)
        norm_x = (lon - self.min_x) / (self.max_x - self.min_x)
        norm_y = (self.max_y - y_merc) / (self.max_y - self.min_y)

        draw_w = self.width - 2 * self.padding
        draw_h = self.height - 2 * self.padding
        return (self.padding + norm_x * draw_w, self.padding + norm_y * draw_h)

# --- GEOMETRİ ---
def get_segment_normal(p1, p2):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    return (0, 0) if length == 0 else (-dy / length, dx / length)

def offset_polyline(points, offset_dist):
    if len(points) < 2: return points
    offset_points = []
    for i in range(len(points)):
        if i == 0: nx, ny = get_segment_normal(points[0], points[1])
        elif i == len(points) - 1: nx, ny = get_segment_normal(points[-2], points[-1])
        else:
            n1, n2 = get_segment_normal(points[i - 1], points[i]), get_segment_normal(points[i], points[i + 1])
            nx, ny = (n1[0] + n2[0]) / 2, (n1[1] + n2[1]) / 2
            norm = math.hypot(nx, ny)
            if norm > 0: nx, ny = nx / norm, ny / norm
        offset_points.append((points[i][0] + nx * offset_dist, points[i][1] + ny * offset_dist))
    return offset_points

def draw_dashed_polyline(surface, color, points, dash_len, space_len, width=1):
    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i + 1]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        seg_dist = math.hypot(dx, dy)
        if seg_dist == 0: continue
        ux, uy = dx / seg_dist, dy / seg_dist
        curr = 0.0
        while curr < seg_dist:
            end = min(curr + dash_len, seg_dist)
            pygame.draw.line(surface, color, (p1[0] + ux * curr, p1[1] + uy * curr), (p1[0] + ux * end, p1[1] + uy * end), width)
            curr += dash_len + space_len
def draw_zebra_crossing(surface, scr_pt, ux, uy, road_width_px, px_per_meter):
    # Yolun gidiş yönüne (ux, uy) 90 derece dik olan normal vektörünü bul (nx, ny)
    nx, ny = -uy, ux
    
    # Zebra şeritlerinin boyutları (Dünya standartlarında ~3m uzunluk, 0.5m genişlik)
    stripe_len = max(2, int(3.0 * px_per_meter))
    stripe_w = max(1, int(0.5 * px_per_meter))
    gap_w = max(1, int(0.5 * px_per_meter))

    # Çizime yolun bir kenarından başla
    half_w = road_width_px / 2.0
    start_x = scr_pt[0] - nx * half_w
    start_y = scr_pt[1] - ny * half_w

    curr = 0
    # Yolun genişliği boyunca şeritleri aralıklarla diz
    while curr < road_width_px:
        c_x = start_x + nx * curr
        c_y = start_y + ny * curr

        s_x1 = c_x - ux * (stripe_len / 2)
        s_y1 = c_y - uy * (stripe_len / 2)
        s_x2 = c_x + ux * (stripe_len / 2)
        s_y2 = c_y + uy * (stripe_len / 2)

        pygame.draw.line(surface, (230, 235, 240), (s_x1, s_y1), (s_x2, s_y2), stripe_w)
        curr += (stripe_w + gap_w)
def get_int(val, default=0):
    try: return int(val)
    except (TypeError, ValueError): return default

# --- GEOMETRİK YUMUŞATMA, KAVŞAK (Y-JUNCTION) VE TAPER MOTORU ---
def generate_junctions_and_tapers(raw_roads, projector):
    node_to_roads = {}
    for idx, r in enumerate(raw_roads):
        n_start = r["nodes"][0]
        n_end = r["nodes"][-1]
        node_to_roads.setdefault(n_start, []).append((idx, True))   # True: Başlangıç
        node_to_roads.setdefault(n_end, []).append((idx, False))  # False: Bitiş

    taper_plans = []
    junction_polys = []

    for nid, conns in node_to_roads.items():
        # =========================================================================
        # 1. DURUM: 3 veya Daha Fazla Yolun Birleştiği Kavşaklar (Y-Kavşak, Çatallanma)
        # =========================================================================
        if len(conns) >= 3:
            # Düğüm noktasının koordinatını al
            idx0, is_s0 = conns[0]
            p_node = raw_roads[idx0]["points"][0] if is_s0 else raw_roads[idx0]["points"][-1]

            max_w_at_node = max(raw_roads[idx]["width"] for idx, _ in conns)
            mouth_corners = []

            # Her yolu kavşak merkezinden dışarıya doğru geri çek (Setback)
            for idx, is_start in conns:
                r = raw_roads[idx]
                pts = r["points"]
                if len(pts) < 2:
                    continue

                p_curr = pts[0] if is_start else pts[-1]
                p_next = pts[1] if is_start else pts[-2]

                dx = p_next[0] - p_curr[0]
                dy = p_next[1] - p_curr[1]
                seg_len = math.hypot(dx, dy)
                if seg_len < 1e-4:
                    continue

                ux, uy = dx / seg_len, dy / seg_len
                # Geri çekilme mesafesi (Yolun genişliğine ve kavşaktaki en geniş yola orantılı)
                setback = min(max(r["width"] * 0.75, max_w_at_node * 0.5), seg_len * 0.42)

                p_cut = (p_curr[0] + ux * setback, p_curr[1] + uy * setback)

                # Yolun uç noktasını geri çekilmiş noktaya sabitle (Şeritler kavşağa taşmaz!)
                if is_start:
                    pts[0] = p_cut
                else:
                    pts[-1] = p_cut

                # Yol ağzındaki sol ve sağ köşe noktalarını hesapla
                half_w = r["width"] / 2.0
                nx, ny = -uy, ux  # Gidiş yönüne dik normal vektör

                p_left = (p_cut[0] - nx * half_w, p_cut[1] - ny * half_w)
                p_right = (p_cut[0] + nx * half_w, p_cut[1] + ny * half_w)

                mouth_corners.append({"pt": p_left, "road_idx": idx, "side": "left"})
                mouth_corners.append({"pt": p_right, "road_idx": idx, "side": "right"})

            if len(mouth_corners) >= 6:
                # Köşeleri kavşak merkezine göre açısal (saat yönünde) sırala
                def get_angle(c):
                    return math.atan2(c["pt"][1] - p_node[1], c["pt"][0] - p_node[0])

                sorted_corners = sorted(mouth_corners, key=get_angle)
                poly_pts = [c["pt"] for c in sorted_corners]

                # Kaldırım/Bordür kenarlarını belirle:
                # İki nokta AYNI yola aitse orası yolun açık ağzıdır (çizgi çekilmez).
                # İki nokta FARKLI yollara aitse orası iki yol arasındaki kaldırımdır (çizgi çekilir).
                curb_lines = []
                n_pts = len(sorted_corners)
                for i in range(n_pts):
                    c1 = sorted_corners[i]
                    c2 = sorted_corners[(i + 1) % n_pts]
                    if c1["road_idx"] != c2["road_idx"]:
                        curb_lines.append((c1["pt"], c2["pt"]))

                xs = [p[0] for p in poly_pts]
                ys = [p[1] for p in poly_pts]

                is_unpaved = any(raw_roads[idx].get("is_unpaved") for idx, _ in conns)
                is_tunnel = all(raw_roads[idx].get("is_tunnel") for idx, _ in conns)
                asphalt_color = (130, 115, 95) if is_unpaved else ((20, 22, 24) if is_tunnel else (55, 58, 64))
                border_color = (100, 85, 65) if is_unpaved else ((40, 45, 50) if is_tunnel else (100, 105, 115))

                junction_polys.append({
                    "poly": poly_pts,
                    "curbs": curb_lines,
                    "asphalt_color": asphalt_color,
                    "border_color": border_color,
                    "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)
                })
            continue

        # =========================================================================
        # 2. DURUM: 2 Yol Arasındaki Şerit Değişimi (Taper - Konik Genişleme)
        # =========================================================================
        if len(conns) == 2:
            (idx1, is_start1), (idx2, is_start2) = conns
            r1, r2 = raw_roads[idx1], raw_roads[idx2]

            if r1["width"] != r2["width"]:
                if r1["width"] > r2["width"]:
                    r_wide, is_start_w, r_narrow, is_start_n = r1, is_start1, r2, is_start2
                else:
                    r_wide, is_start_w, r_narrow, is_start_n = r2, is_start2, r1, is_start1

                pts_w = r_wide["points"]
                if len(pts_w) < 2:
                    continue

                p_junc = pts_w[0] if is_start_w else pts_w[-1]
                p_next = pts_w[1] if is_start_w else pts_w[-2]

                dx = p_next[0] - p_junc[0]
                dy = p_next[1] - p_junc[1]
                seg_len = math.hypot(dx, dy)
                if seg_len < 1e-4:
                    continue

                ux, uy = dx / seg_len, dy / seg_len
                taper_dist = min(20.0 * projector.units_per_meter, seg_len * 0.45)
                p_taper = (p_junc[0] + ux * taper_dist, p_junc[1] + uy * taper_dist)

                if is_start_w:
                    pts_w[0] = p_taper
                else:
                    pts_w[-1] = p_taper

                taper_plans.append({
                    "r_wide": r_wide, "is_start_w": is_start_w,
                    "r_narrow": r_narrow, "is_start_n": is_start_n,
                    "p_junc": p_junc, "p_taper": p_taper
                })

    return taper_plans, junction_polys


# --- GÜNCELLENMİŞ PARÇALI VERİ AYRIŞTIRICI ---
def parse_osm_chunks(chunk_files, projector):
    print("[SİSTEM] Tüm parçalar birleştiriliyor ve işleniyor...")
    raw_roads = []
    buildings = []
    seen_ways = set()
    crossings = set()

    for filename in chunk_files:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)

        nodes = {elem["id"]: (elem["lat"], elem["lon"]) for elem in data.get("elements", []) if elem["type"] == "node"}

        # 1. Yaya Geçitlerini Topla
        for elem in data.get("elements", []):
            if elem["type"] == "node" and elem.get("tags", {}).get("highway") == "crossing":
                crossings.add(elem["id"])

        # 2. Yolları ve Binaları Oku
        for elem in data.get("elements", []):
            if elem["type"] == "way":
                way_id = elem["id"]
                if way_id in seen_ways:
                    continue
                seen_ways.add(way_id)

                tags = elem.get("tags", {})
                points = [projector.project(*nodes[nid]) for nid in elem.get("nodes", []) if nid in nodes]
                if len(points) < 2:
                    continue

                xs = [p[0] for p in points]
                ys = [p[1] for p in points]

                # BİNA İŞLEME
                if "building" in tags:
                    if len(points) >= 3:
                        b_type = tags.get("building", "yes")
                        levels = get_int(tags.get("building:levels", 1))
                        height_meters = get_int(tags.get("height", levels * 3))

                        ind_tags = ["industrial", "commercial", "retail", "office", "warehouse", "manufacture"]
                        res_tags = ["residential", "apartments", "house", "dormitory", "terrace", "detached"]

                        category = "default"
                        if b_type in ind_tags: category = "industrial"
                        elif b_type in res_tags: category = "residential"

                        buildings.append({
                            "points": points, "category": category,
                            "levels": levels, "height_m": height_meters,
                            "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)
                        })

                # YOL İŞLEME
                elif "highway" in tags:
                    hw_type = tags.get("highway")
                    if hw_type in ["footway", "pedestrian", "path", "steps", "cycleway"]:
                        continue

                    surface = tags.get("surface", "unknown")
                    unpaved_surfaces = ["dirt", "unpaved", "gravel", "earth", "ground", "sand", "grass", "mud", "compacted"]
                    is_unpaved = (hw_type == "track") or (surface in unpaved_surfaces)

                    is_oneway = tags.get("oneway") in ["yes", "1", "true"]
                    lanes = get_int(tags.get("lanes:forward", 0)) + get_int(tags.get("lanes:backward", 0))
                    if lanes == 0:
                        lanes = get_int(tags.get("lanes", 0))
                        if lanes == 0:
                            per_dir = {"motorway": 3, "trunk": 3, "primary": 2, "secondary": 2}.get(hw_type, 1)
                            lanes = per_dir if is_oneway else per_dir * 2
                    lanes = max(1, lanes)

                    is_bridge = tags.get("bridge") in ["yes", "true", "1", "viaduct"]
                    is_tunnel = tags.get("tunnel") in ["yes", "true", "1", "building_passage"]
                    z_index = 1 if is_bridge else (-1 if is_tunnel else 0)

                    world_lane_w = (2.5 if is_unpaved else 3.5) * projector.units_per_meter
                    total_w = lanes * world_lane_w

                    raw_roads.append({
                        "points": points,
                        "nodes": elem.get("nodes", []),
                        "lanes": lanes,
                        "width": total_w,
                        "lane_w": world_lane_w,
                        "type": hw_type,
                        "is_oneway": is_oneway,
                        "is_bridge": is_bridge,
                        "is_tunnel": is_tunnel,
                        "is_unpaved": is_unpaved,
                        "z_index": z_index
                    })

    # --- KAVŞAK VE TAPERLAR HESAPLANIYOR (Uçlar düzeltiliyor) ---
    taper_plans, junction_polys = generate_junctions_and_tapers(raw_roads, projector)

    # 3. Yolların Bordür ve Şeritlerini Hesapla (Trimlenmiş noktalar üzerinden!)
    roads = []
    for r in raw_roads:
        pts = r["points"]
        total_w = r["width"]
        half_w = total_w / 2.0
        world_lane_w = r["lane_w"]
        lanes = r["lanes"]

        left_border = offset_polyline(pts, -half_w)
        right_border = offset_polyline(pts, half_w)

        dividers = []
        if lanes > 1 and not r["is_unpaved"]:
            for lane_idx in range(1, lanes):
                offset = -half_w + (lane_idx * world_lane_w)
                div_pts = offset_polyline(pts, offset)
                if len(div_pts) >= 2:
                    is_center = False
                    if not r["is_oneway"]:
                        if lanes % 2 == 0 and lane_idx == lanes // 2: is_center = True
                        elif lanes % 2 != 0 and lane_idx == lanes // 2: is_center = True
                    dividers.append({"pts": div_pts, "is_center": is_center})

        # Yaya geçitleri
        road_crossings = []
        node_ids = r["nodes"]
        for i, nid in enumerate(node_ids):
            if nid in crossings and i < len(pts):
                p_curr = pts[i]
                if i < len(pts) - 1:
                    dx, dy = pts[i+1][0] - p_curr[0], pts[i+1][1] - p_curr[1]
                elif i > 0:
                    dx, dy = p_curr[0] - pts[i-1][0], p_curr[1] - pts[i-1][1]
                else:
                    dx, dy = 1, 0
                length = math.hypot(dx, dy)
                if length > 0:
                    road_crossings.append({"pt": p_curr, "dir": (dx/length, dy/length)})

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]

        r.update({
            "body": pts, "left": left_border, "right": right_border, "dividers": dividers,
            "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys),
            "crossings": road_crossings
        })
        roads.append(r)

    # 4. Taper Geometrilerini Bordür Noktalarına Mühürle
    tapers = []
    for plan in taper_plans:
        r_w, is_s_w = plan["r_wide"], plan["is_start_w"]
        r_n, is_s_n = plan["r_narrow"], plan["is_start_n"]

        l_junc = r_n["left"][0 if is_s_n else -1]
        r_junc = r_n["right"][0 if is_s_n else -1]

        l_taper = r_w["left"][0 if is_s_w else -1]
        r_taper = r_w["right"][0 if is_s_w else -1]

        if math.hypot(l_junc[0] - l_taper[0], l_junc[1] - l_taper[1]) > math.hypot(l_junc[0] - r_taper[0], l_junc[1] - r_taper[1]):
            l_taper, r_taper = r_taper, l_taper

        poly = [l_junc, l_taper, r_taper, r_junc]
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]

        is_tunnel = r_w.get("is_tunnel", False)
        is_unpaved = r_w.get("is_unpaved", False)
        asphalt_color = (130, 115, 95) if is_unpaved else ((20, 22, 24) if is_tunnel else (55, 58, 64))
        border_color = (100, 85, 65) if is_unpaved else ((40, 45, 50) if is_tunnel else (100, 105, 115))

        has_center = (not r_w.get("is_oneway", True)) and (not r_n.get("is_oneway", True))

        tapers.append({
            "poly": poly,
            "left": [l_junc, l_taper],
            "right": [r_junc, r_taper],
            "asphalt_color": asphalt_color,
            "border_color": border_color,
            "is_center": has_center,
            "center_pts": [plan["p_junc"], plan["p_taper"]],
            "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)
        })

    print(f"[SİSTEM] {len(roads)} Yol, {len(junction_polys)} Kavşak Alanı, {len(tapers)} Taper, {len(buildings)} Bina hazır.")
    return roads, buildings, tapers, junction_polys


# --- ANA ÇİZİM DÖNGÜSÜ ---
def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption("ANKARA HARİTA RENDERER (Pürüzsüz Kavşaklar & Çatallanmalar)")
    clock = pygame.time.Clock()

    projector = MapProjector(BBOX, SCREEN_WIDTH, SCREEN_HEIGHT)
    chunk_files = fetch_osm_chunks(BBOX, GRID_SIZE)
    roads, buildings, tapers, junction_polys = parse_osm_chunks(chunk_files, projector)
    roads.sort(key=lambda r: (r.get("z_index", 0), r["lanes"]))

    camera_x, camera_y, zoom = 0.0, 0.0, 1.0
    is_dragging = False
    last_mouse_pos = (0, 0)

    running = True
    while running:
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    is_dragging = True
                    last_mouse_pos = mouse_pos
                elif event.button in [4, 5]:
                    zoom_factor = 1.1 if event.button == 4 else 0.9
                    world_x, world_y = (mouse_pos[0] / zoom) - camera_x, (mouse_pos[1] / zoom) - camera_y
                    zoom = max(0.1, min(zoom * zoom_factor, 1000.0))
                    camera_x, camera_y = (mouse_pos[0] / zoom) - world_x, (mouse_pos[1] / zoom) - world_y
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                is_dragging = False
            elif event.type == pygame.MOUSEMOTION and is_dragging:
                dx, dy = mouse_pos[0] - last_mouse_pos[0], mouse_pos[1] - last_mouse_pos[1]
                camera_x += dx / zoom
                camera_y += dy / zoom
                last_mouse_pos = mouse_pos

        view_min_x, view_max_x = -camera_x, -camera_x + (SCREEN_WIDTH / zoom)
        view_min_y, view_max_y = -camera_y, -camera_y + (SCREEN_HEIGHT / zoom)

        screen.fill((20, 22, 26))

        def to_screen(pts):
            return [((x + camera_x) * zoom, (y + camera_y) * zoom) for x, y in pts]

        px_per_meter = projector.units_per_meter * zoom

        # 1. Binalar (En alt katman)
        if zoom > 10.0:
            for b in buildings:
                if (b["max_x"] < view_min_x or b["min_x"] > view_max_x or
                    b["max_y"] < view_min_y or b["min_y"] > view_max_y):
                    continue
                scr_pts = to_screen(b["points"])
                if len(scr_pts) > 2:
                    if b["category"] == "industrial":
                        color, border = (190, 110, 50), (130, 70, 30)
                    elif b["category"] == "residential":
                        color, border = (180, 185, 180), (120, 125, 120)
                    else:
                        color, border = (80, 85, 90), (50, 55, 60)

                    pygame.draw.polygon(screen, color, scr_pts)
                    pygame.draw.polygon(screen, border, scr_pts, max(1, int(0.2 * px_per_meter)))

        # 2. Kavşak Alanları (Y-Kavşak ve Çatallanma Gövdeleri)
        for junc in junction_polys:
            if (junc["max_x"] < view_min_x or junc["min_x"] > view_max_x or
                junc["max_y"] < view_min_y or junc["min_y"] > view_max_y):
                continue
            scr_poly = to_screen(junc["poly"])
            if len(scr_poly) >= 3:
                pygame.draw.polygon(screen, junc["asphalt_color"], scr_poly)

        # 3. Konik Geçiş (Taper) Gövdeleri
        for taper in tapers:
            if (taper["max_x"] < view_min_x or taper["min_x"] > view_max_x or
                taper["max_y"] < view_min_y or taper["min_y"] > view_max_y):
                continue
            scr_poly = to_screen(taper["poly"])
            pygame.draw.polygon(screen, taper["asphalt_color"], scr_poly)

        # 4. Yolların Asfalt Gövdeleri
        roads_drawn = 0
        for road in roads:
            if (road["max_x"] < view_min_x or road["min_x"] > view_max_x or
                road["max_y"] < view_min_y or road["min_y"] > view_max_y):
                continue

            if zoom < 0.4 and road["type"] in ["residential", "service", "unclassified"]:
                continue
            roads_drawn += 1

            scaled_width = road["width"] * zoom
            scr_body = to_screen(road["body"])
            if len(scr_body) < 2:
                continue

            is_bridge = road.get("is_bridge", False)
            is_tunnel = road.get("is_tunnel", False)
            is_unpaved = road.get("is_unpaved", False)
            asphalt_color = (130, 115, 95) if is_unpaved else ((20, 22, 24) if is_tunnel else (55, 58, 64))

            if is_bridge and zoom > 10.0:
                shadow_body = [((x + camera_x) * zoom + 5, (y + camera_y) * zoom + 5) for x, y in road["body"]]
                pygame.draw.lines(screen, (15, 16, 18), False, shadow_body, max(1, int(scaled_width)))

            if is_bridge:
                pygame.draw.lines(screen, (150, 155, 160), False, scr_body, max(1, int(scaled_width + (1.0 * px_per_meter))))

            pygame.draw.lines(screen, asphalt_color, False, scr_body, max(1, int(scaled_width)))

        # 5. Bordür Çizgileri ve Şeritler
        if zoom > 5.0:
            # Kavşak Bordürleri (Yollar arasındaki bordür yayları / adacık kenarları)
            for junc in junction_polys:
                if (junc["max_x"] < view_min_x or junc["min_x"] > view_max_x or
                    junc["max_y"] < view_min_y or junc["min_y"] > view_max_y):
                    continue
                for p1, p2 in junc["curbs"]:
                    scr_p1 = to_screen([p1])[0]
                    scr_p2 = to_screen([p2])[0]
                    pygame.draw.line(screen, junc["border_color"], scr_p1, scr_p2, 1)

            # Taper bordürleri ve merkez sarı çizgisi
            for taper in tapers:
                if (taper["max_x"] < view_min_x or taper["min_x"] > view_max_x or
                    taper["max_y"] < view_min_y or taper["min_y"] > view_max_y):
                    continue
                scr_left = to_screen(taper["left"])
                scr_right = to_screen(taper["right"])
                pygame.draw.line(screen, taper["border_color"], scr_left[0], scr_left[1], 1)
                pygame.draw.line(screen, taper["border_color"], scr_right[0], scr_right[1], 1)

                if taper.get("is_center"):
                    scr_c = to_screen(taper["center_pts"])
                    outer_w, inner_w = max(4, int(0.7 * px_per_meter)), max(2, int(0.25 * px_per_meter))
                    pygame.draw.lines(screen, (235, 185, 30), False, scr_c, outer_w)
                    pygame.draw.lines(screen, taper["asphalt_color"], False, scr_c, inner_w)

            # Yol bordürleri ve iç şeritler
            for road in roads:
                if (road["max_x"] < view_min_x or road["min_x"] > view_max_x or
                    road["max_y"] < view_min_y or road["min_y"] > view_max_y):
                    continue

                is_tunnel = road.get("is_tunnel", False)
                is_unpaved = road.get("is_unpaved", False)
                asphalt_color = (130, 115, 95) if is_unpaved else ((20, 22, 24) if is_tunnel else (55, 58, 64))
                border_color = (100, 85, 65) if is_unpaved else ((40, 45, 50) if is_tunnel else (100, 105, 115))

                if len(road["left"]) >= 2: pygame.draw.lines(screen, border_color, False, to_screen(road["left"]), 1)
                if len(road["right"]) >= 2: pygame.draw.lines(screen, border_color, False, to_screen(road["right"]), 1)

                if road["type"] not in ["residential", "unclassified", "living_street", "service"] and not is_unpaved:
                    for div in road["dividers"]:
                        scr_div = to_screen(div["pts"])
                        if div["is_center"]:
                            c_color = (130, 100, 20) if is_tunnel else (235, 185, 30)
                            outer_w, inner_w = max(4, int(0.7 * px_per_meter)), max(2, int(0.25 * px_per_meter))
                            pygame.draw.lines(screen, c_color, False, scr_div, outer_w)
                            pygame.draw.lines(screen, asphalt_color, False, scr_div, inner_w)
                        else:
                            c_color = (80, 85, 90) if is_tunnel else (200, 205, 210)
                            dash_px = max(4.0, 3.0 * px_per_meter)
                            space_px = max(4.0, 3.0 * px_per_meter)
                            l_width = max(1, int(0.15 * px_per_meter))
                            draw_dashed_polyline(screen, c_color, scr_div, dash_px, space_px, l_width)

        # 6. Yaya Geçitleri
        if zoom > 15.0:
            for road in roads:
                if (road["max_x"] < view_min_x or road["min_x"] > view_max_x or
                    road["max_y"] < view_min_y or road["min_y"] > view_max_y):
                    continue
                scaled_width = road["width"] * zoom
                for cx in road.get("crossings", []):
                    scr_pt = to_screen([cx["pt"]])[0]
                    draw_zebra_crossing(screen, scr_pt, cx["dir"][0], cx["dir"][1], scaled_width, px_per_meter)

        # Bilgi Ekranı
        font = pygame.font.SysFont("Consolas", 14)
        screen.blit(font.render(f"FPS: {clock.get_fps():.1f} | Zoom: {zoom:.2f} | Yol: {roads_drawn} | Kavşak: {len(junction_polys)}", True, (255, 255, 255)), (10, 10))
        screen.blit(font.render("Y-Kavşak & Çatallanma Geometrisi Düzeltildi", True, (100, 220, 120)), (10, 30))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
if __name__ == "__main__":
    main()