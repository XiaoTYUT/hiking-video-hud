"""KML 路径解析 Demo

解析 KML 文件中的 LineString/Coordinates，提取经纬度路径点并打印。
仅使用 Python 标准库，零依赖。
"""

import xml.etree.ElementTree as ET
from pathlib import Path


def parse_kml_coords(kml_path: str | Path) -> list[tuple[float, float, float]]:
    """从 KML 文件中提取所有坐标点 (lon, lat, elevation)。"""
    tree = ET.parse(kml_path)
    root = tree.getroot()

    # KML 使用命名空间
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    points: list[tuple[float, float, float]] = []
    for coords_elem in root.iterfind(".//kml:coordinates", ns):
        text = coords_elem.text or ""
        for token in text.strip().split():
            parts = token.split(",")
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                elev = float(parts[2]) if len(parts) >= 3 else 0.0
                points.append((lon, lat, elev))

    return points


def main() -> None:
    sample = Path(__file__).parent / "sample.kml"
    if not sample.exists():
        print(f"找不到示例文件: {sample}")
        return

    print(f"解析 KML: {sample}")
    points = parse_kml_coords(sample)
    print(f"共 {len(points)} 个坐标点:\n")

    for i, (lon, lat, elev) in enumerate(points):
        print(f"  [{i:02d}] 经度={lon:.6f}, 纬度={lat:.6f}, 海拔={elev:.1f}m")


if __name__ == "__main__":
    main()
