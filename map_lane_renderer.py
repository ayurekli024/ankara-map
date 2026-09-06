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

# --- 2. PARÇALI VERİ AYRIŞTIRICI VE TEKİLLEŞTİRME ---
def parse_osm_chunks(chunk_files, projector):
    print("[SİSTEM] Tüm parçalar birleştiriliyor ve işleniyor...")
    roads = []
    buildings = []
    seen_ways = set() # Mükerrer (kesişen) binaları/yolları engellemek için
    
    LANE_WIDTH_WORLD = 3.5 * projector.units_per_meter
    crossings = set()
    for filename in chunk_files:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        nodes = {elem["id"]: (elem["lat"], elem["lon"]) for elem in data.get("elements", []) if elem["type"] == "node"}
        # 1. Aşama: Yaya geçidi noktalarının ID'lerini topla
        for elem in data.get("elements", []):
            if elem["type"] == "node" and elem.get("tags", {}).get("highway") == "crossing":
                crossings.add(elem["id"])
                
        # 2. Aşama: Yolları ve Binaları İşle
        for elem in data.get("elements", []):
            if elem["type"] == "way":
                way_id = elem["id"]
                
                # Sınır kesişmelerinde aynı bina/yol iki kere çizilmesin
                if way_id in seen_ways:
                    continue
                seen_ways.add(way_id)
                
                tags = elem.get("tags", {})
                points = [projector.project(*nodes[nid]) for nid in elem.get("nodes", []) if nid in nodes]
                if len(points) < 2: continue
                    
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]

                # BİNA İŞLEME
                if "building" in tags:
                    if len(points) >= 3:
                        b_type = tags.get("building", "yes")
                        ind_tags = ["industrial", "commercial", "retail", "office", "warehouse", "manufacture"]
                        res_tags = ["residential", "apartments", "house", "dormitory", "terrace", "detached"]
                        
                        category = "default"
                        if b_type in ind_tags: category = "industrial"
                        elif b_type in res_tags: category = "residential"

                        buildings.append({
                            "points": points, "category": category,
                            "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)
                        })

                # YOL İŞLEME
                elif "highway" in tags:
                    hw_type = tags.get("highway")
                    if hw_type in ["footway", "pedestrian", "path", "steps", "cycleway"]: continue

                    is_oneway = tags.get("oneway") in ["yes", "1", "true"]
                    lanes = get_int(tags.get("lanes:forward", 0)) + get_int(tags.get("lanes:backward", 0))
                    
                    if lanes == 0:
                        lanes = get_int(tags.get("lanes", 0))
                        if lanes == 0:
                            per_dir = {"motorway":3, "trunk":3, "primary":2, "secondary":2}.get(hw_type, 1)
                            lanes = per_dir if is_oneway else per_dir * 2
                    lanes = max(1, lanes)
                    
                    is_bridge = tags.get("bridge") in ["yes", "true", "1", "viaduct"]
                    is_tunnel = tags.get("tunnel") in ["yes", "true", "1", "building_passage"]
                    z_index = 1 if is_bridge else (-1 if is_tunnel else 0)
                    
                    total_w = lanes * LANE_WIDTH_WORLD
                    half_w = total_w / 2.0
                    left_border = offset_polyline(points, -half_w)
                    right_border = offset_polyline(points, half_w)
                    
                    dividers = []
                    if lanes > 1:
                        for lane_idx in range(1, lanes):
                            offset = -half_w + (lane_idx * LANE_WIDTH_WORLD)
                            div_pts = offset_polyline(points, offset)
                            if len(div_pts) >= 2:
                                is_center = False
                                if not is_oneway:
                                    if lanes % 2 == 0 and lane_idx == lanes // 2: is_center = True
                                    elif lanes % 2 != 0 and lane_idx == lanes // 2: is_center = True
                                dividers.append({"pts": div_pts, "is_center": is_center})

                    # YENİ: Yol üzerindeki yaya geçitlerinin yönünü ve konumunu hesapla
                    road_crossings = []
                    node_ids = elem.get("nodes", [])
                    for i, nid in enumerate(node_ids):
                        if nid in crossings:
                            p_curr = points[i]
                            # Yolun o anki teğet yönünü (vektörünü) bul
                            if i < len(points) - 1:
                                dx, dy = points[i+1][0] - p_curr[0], points[i+1][1] - p_curr[1]
                            elif i > 0:
                                dx, dy = p_curr[0] - points[i-1][0], p_curr[1] - points[i-1][1]
                            else:
                                dx, dy = 1, 0
                                
                            length = math.hypot(dx, dy)
                            if length > 0:
                                road_crossings.append({"pt": p_curr, "dir": (dx/length, dy/length)})

                    # append kısmına "crossings" eklendi
                    roads.append({
                        "body": points, "left": left_border, "right": right_border, "dividers": dividers,
                        "lanes": lanes, "type": hw_type, "width": total_w,
                        "min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys),
                        "is_bridge": is_bridge, "is_tunnel": is_tunnel, "z_index": z_index,
                        "crossings": road_crossings # <--- EKLENDİ
                    })

    print(f"[SİSTEM] Başarıyla Birleştirildi! Toplam: {len(roads)} Yol, {len(buildings)} Bina.")
    return roads, buildings

# --- ANA DÖNGÜ ---
def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption("ANKARA HARİTA RENDERER (OSM Verisi)")
    clock = pygame.time.Clock()

    # Eski: roads, buildings = parse_osm_data(fetch_osm_data(BBOX), projector)
    # YENİ ÇAĞIRMA BİÇİMİ:
    projector = MapProjector(BBOX, SCREEN_WIDTH, SCREEN_HEIGHT)
    chunk_files = fetch_osm_chunks(BBOX, GRID_SIZE)
    roads, buildings = parse_osm_chunks(chunk_files, projector)
    roads.sort(key=lambda r: (r.get("z_index", 0), r["lanes"]))

    camera_x, camera_y, zoom = 0.0, 0.0, 1.0
    is_dragging = False
    last_mouse_pos = (0, 0)

    running = True
    while running:
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT: running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    is_dragging = True
                    last_mouse_pos = mouse_pos
                elif event.button in [4, 5]:
                    zoom_factor = 1.1 if event.button == 4 else 0.9
                    world_x, world_y = (mouse_pos[0] / zoom) - camera_x, (mouse_pos[1] / zoom) - camera_y
                    zoom = max(0.1, min(zoom * zoom_factor, 1000.0))
                    camera_x, camera_y = (mouse_pos[0] / zoom) - world_x, (mouse_pos[1] / zoom) - world_y
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1: is_dragging = False
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

        # --- 1. BİNALARI ÇİZ (En alt katman, yolların altında kalır) ---
        buildings_drawn = 0
        
        # YENİ OPTİMİZASYON: Zoom seviyesi 10'un altındaysa bina döngüsüne HİÇ girme.
        # Bu sayede oyun ilk yüklendiğinde yüz binlerce bina sorgusu atlanıp FPS korunur.
        if zoom > 10.0:
            for b in buildings:
                # Culling: Bina kameranın görüş alanı (frustum) dışındaysa atla
                if (b["max_x"] < view_min_x or b["min_x"] > view_max_x or
                    b["max_y"] < view_min_y or b["min_y"] > view_max_y):
                    continue
                
                scr_pts = to_screen(b["points"])
                if len(scr_pts) > 2:
                    # RENGİ BELİRLEME
                    if b["category"] == "industrial":
                        color = (190, 110, 50)  
                        border = (130, 70, 30)
                    elif b["category"] == "residential":
                        color = (180, 185, 180) 
                        border = (120, 125, 120)
                    else:
                        color = (80, 85, 90)    
                        border = (50, 55, 60)

                    pygame.draw.polygon(screen, color, scr_pts)
                    pygame.draw.polygon(screen, border, scr_pts, max(1, int(0.2 * px_per_meter)))
                    buildings_drawn += 1

        # --- 2. YOLLARI ÇİZ ---
        roads_drawn = 0
        for road in roads:
            if (road["max_x"] < view_min_x or road["min_x"] > view_max_x or
                road["max_y"] < view_min_y or road["min_y"] > view_max_y):
                continue
                
            if zoom < 0.4 and road["type"] in ["residential", "service", "unclassified"]: continue
            roads_drawn += 1

            scaled_width = road["width"] * zoom
            scr_body = to_screen(road["body"])
            if len(scr_body) < 2: continue

            is_bridge, is_tunnel = road.get("is_bridge", False), road.get("is_tunnel", False)
            asphalt_color = (20, 22, 24) if is_tunnel else (55, 58, 64)
            border_color = (40, 45, 50) if is_tunnel else (100, 105, 115)

            if is_bridge and zoom > 10.0:
                shadow_body = [((x + camera_x) * zoom + 5, (y + camera_y) * zoom + 5) for x, y in road["body"]]
                pygame.draw.lines(screen, (15, 16, 18), False, shadow_body, max(1, int(scaled_width)))

            if is_bridge:
                pygame.draw.lines(screen, (150, 155, 160), False, scr_body, max(1, int(scaled_width + (1.0 * px_per_meter))))

            pygame.draw.lines(screen, asphalt_color, False, scr_body, max(1, int(scaled_width)))

            if zoom > 5.0:
                if len(road["left"]) >= 2: pygame.draw.lines(screen, border_color, False, to_screen(road["left"]), 1)
                if len(road["right"]) >= 2: pygame.draw.lines(screen, border_color, False, to_screen(road["right"]), 1)

                if road["type"] not in ["residential", "unclassified", "living_street", "service"]:
                    for div in road["dividers"]:
                        scr_div = to_screen(div["pts"])
                        if div["is_center"]:
                            c_color = (130, 100, 20) if is_tunnel else (235, 185, 30)
                            outer_w, inner_w = max(4, int(0.7 * px_per_meter)), max(2, int(0.25 * px_per_meter))
                            pygame.draw.lines(screen, c_color, False, scr_div, outer_w)
                            pygame.draw.lines(screen, asphalt_color, False, scr_div, inner_w)
                        else:
                            c_color = (80, 85, 90) if is_tunnel else (200, 205, 210)
                            dash_px, space_px, l_width = max(4.0, 3.0 * px_per_meter), max(4.0, 3.0 * px_per_meter), max(1, int(0.15 * px_per_meter))
                            draw_dashed_polyline(screen, c_color, scr_div, dash_px, space_px, l_width)
            # --- 5. YAYA GEÇİTLERİ (Zebra Crossings - Sadece çok yakından görünür) ---
            if zoom > 15.0:
                for cx in road.get("crossings", []):
                    # Ekran koordinatına çevir
                    scr_pt = to_screen([cx["pt"]])[0]
                    # Yaya geçidi fonksiyonunu çağır
                    draw_zebra_crossing(screen, scr_pt, cx["dir"][0], cx["dir"][1], scaled_width, px_per_meter)

        # Bilgi Ekranı
        font = pygame.font.SysFont("Consolas", 14)
        screen.blit(font.render(f"FPS: {clock.get_fps():.1f} | Zoom: {zoom:.2f} | Yol: {roads_drawn} | Bina: {buildings_drawn}", True, (255, 255, 255)), (10, 10))
        screen.blit(font.render("Turuncu: Sanayi/Is Yeri | Acik Gri: Yerlesim/Konut", True, (190, 110, 50)), (10, 30))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()

if __name__ == "__main__":
    main()