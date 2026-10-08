"""高德地图MCP服务封装"""

import json
import requests
import threading
from typing import List, Dict, Any, Optional
from hello_agents.tools import MCPTool
from ..config import get_settings
from ..models.schemas import Location, POIInfo, WeatherInfo

# 全局MCP工具实例
_amap_mcp_tool = None
# 路由层现在会经 asyncio.to_thread 从多个工作线程调用本模块的取实例函数，
# 因此「检查-构造-赋值」这段必须加锁（详见 get_amap_service 的注释）。
_amap_mcp_tool_lock = threading.Lock()


def get_amap_mcp_tool() -> MCPTool:
    """
    获取高德地图MCP工具实例(单例模式)
    
    Returns:
        MCPTool实例
    """
    global _amap_mcp_tool
    
    if _amap_mcp_tool is None:
        with _amap_mcp_tool_lock:
            if _amap_mcp_tool is None:
                settings = get_settings()

                if not settings.vite_amap_web_key:
                    raise ValueError("高德地图 API Key 未配置，请先在前端设置页完成配置")

                # 创建MCP工具
                _amap_mcp_tool = MCPTool(
                    name="amap",
                    description="高德地图服务,支持POI搜索、路线规划、天气查询等功能",
                    server_command=["uvx", "amap-mcp-server"],
                    env={"AMAP_MAPS_API_KEY": settings.vite_amap_web_key},
                    auto_expand=True  # 自动展开为独立工具
                )

                print(f"✅ 高德地图MCP工具初始化成功")
                print(f"   工具数量: {len(_amap_mcp_tool._available_tools)}")

                # 打印可用工具列表
                if _amap_mcp_tool._available_tools:
                    print("   可用工具:")
                    for tool in _amap_mcp_tool._available_tools[:5]:  # 只打印前5个
                        print(f"     - {tool.get('name', 'unknown')}")
                    if len(_amap_mcp_tool._available_tools) > 5:
                        print(f"     ... 还有 {len(_amap_mcp_tool._available_tools) - 5} 个工具")

    return _amap_mcp_tool


class AmapService:
    """高德地图服务封装类"""
    
    def __init__(self):
        """初始化服务"""
        self.mcp_tool = get_amap_mcp_tool()
    
    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """
        搜索POI
        
        Args:
            keywords: 搜索关键词
            city: 城市
            citylimit: 是否限制在城市范围内
            
        Returns:
            POI信息列表
        """
        try:
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_text_search",
                "arguments": {
                    "keywords": keywords,
                    "city": city,
                    "citylimit": str(citylimit).lower()
                }
            })
            
            # 解析MCP返回结果为POIInfo列表
            pois = self._parse_pois_result(result)
            if pois:
                print(f"✅ POI搜索成功(MCP解析): {len(pois)} 条")
                return pois
            print("⚠️ MCP结果解析为空，改用REST直调兜底")

        except Exception as e:
            print(f"⚠️ MCP调用异常，改用REST直调兜底: {str(e)}")

        return self._search_poi_rest(keywords, city, citylimit)

    def _parse_pois_result(self, result: Any) -> List[POIInfo]:
        """解析MCP工具返回的POI数据，兼容字符串/字典多种形态。"""
        data = result
        if isinstance(data, str):
            text = data.strip()
            # 兼容 markdown 代码块包裹的 JSON
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            try:
                data = json.loads(text)
            except Exception:
                print("⚠️ POI结果不是合法JSON，跳过解析")
                return []
        if isinstance(data, dict):
            raw_pois = data.get("pois")
            if isinstance(raw_pois, list):
                pois: List[POIInfo] = []
                for p in raw_pois:
                    info = self._build_poi_info(p)
                    if info is not None:
                        pois.append(info)
                return pois
        return []

    def _search_poi_rest(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """REST直调兜底：直接请求高德 place/text 接口。"""
        try:
            settings = get_settings()
            params = {
                "key": settings.vite_amap_web_key,
                "keywords": keywords,
                "city": city,
                "citylimit": "true" if citylimit else "false",
                "output": "JSON",
                "extensions": "base",
            }
            resp = requests.get("https://restapi.amap.com/v3/place/text", params=params, timeout=20)
            data = resp.json()
            if data.get("infocode") != "10000":
                print(f"❌ 高德REST返回异常: {data.get('info')}({data.get('infocode')})")
                return []
            pois: List[POIInfo] = []
            for p in data.get("pois", []) or []:
                info = self._build_poi_info(p)
                if info is not None:
                    pois.append(info)
            print(f"✅ POI搜索成功(REST直调): {len(pois)} 条")
            return pois
        except Exception as e:
            print(f"❌ POI搜索失败: {str(e)}")
            return []

    @staticmethod
    def _build_poi_info(p: Any) -> Optional[POIInfo]:
        """把单个POI字典转为POIInfo，缺经纬度则跳过该条。"""
        if not isinstance(p, dict):
            return None
        try:
            loc_raw = p.get("location")
            location = None
            if isinstance(loc_raw, str) and "," in loc_raw:
                lng, lat = loc_raw.split(",", 1)
                location = Location(longitude=float(lng), latitude=float(lat))
            elif isinstance(loc_raw, dict):
                lng = loc_raw.get("longitude", loc_raw.get("lng"))
                lat = loc_raw.get("latitude", loc_raw.get("lat"))
                if lng is not None and lat is not None:
                    location = Location(longitude=float(lng), latitude=float(lat))
            if location is None:
                return None
            return POIInfo(
                id=str(p.get("id", "")),
                name=str(p.get("name", "")),
                type=str(p.get("type", "")),
                address=str(p.get("address", "")),
                location=location,
                tel=(str(p.get("tel")) if p.get("tel") else None),
            )
        except (ValueError, TypeError):
            return None
    
    def get_weather(self, city: str) -> List[WeatherInfo]:
        """
        查询天气
        
        Args:
            city: 城市名称
            
        Returns:
            天气信息列表
        """
        try:
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_weather",
                "arguments": {
                    "city": city
                }
            })
            
            print(f"天气查询结果: {result[:200]}...")
            
            # TODO: 解析实际的天气数据
            return []
            
        except Exception as e:
            print(f"❌ 天气查询失败: {str(e)}")
            return []
    
    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking"
    ) -> Dict[str, Any]:
        """
        规划路线
        
        Args:
            origin_address: 起点地址
            destination_address: 终点地址
            origin_city: 起点城市
            destination_city: 终点城市
            route_type: 路线类型 (walking/driving/transit)
            
        Returns:
            路线信息
        """
        try:
            # 根据路线类型选择工具
            tool_map = {
                "walking": "maps_direction_walking_by_address",
                "driving": "maps_direction_driving_by_address",
                "transit": "maps_direction_transit_integrated_by_address"
            }
            
            tool_name = tool_map.get(route_type, "maps_direction_walking_by_address")
            
            # 构建参数
            arguments = {
                "origin_address": origin_address,
                "destination_address": destination_address
            }
            
            # 公共交通需要城市参数
            if route_type == "transit":
                if origin_city:
                    arguments["origin_city"] = origin_city
                if destination_city:
                    arguments["destination_city"] = destination_city
            else:
                # 其他路线类型也可以提供城市参数提高准确性
                if origin_city:
                    arguments["origin_city"] = origin_city
                if destination_city:
                    arguments["destination_city"] = destination_city
            
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": tool_name,
                "arguments": arguments
            })
            
            print(f"路线规划结果: {result[:200]}...")
            
            # TODO: 解析实际的路线数据
            return {}
            
        except Exception as e:
            print(f"❌ 路线规划失败: {str(e)}")
            return {}
    
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """
        地理编码(地址转坐标)

        Args:
            address: 地址
            city: 城市

        Returns:
            经纬度坐标
        """
        try:
            arguments = {"address": address}
            if city:
                arguments["city"] = city

            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_geo",
                "arguments": arguments
            })

            print(f"地理编码结果: {result[:200]}...")

            # TODO: 解析实际的坐标数据
            return None

        except Exception as e:
            print(f"❌ 地理编码失败: {str(e)}")
            return None

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """
        获取POI详情

        Args:
            poi_id: POI ID

        Returns:
            POI详情信息
        """
        try:
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_search_detail",
                "arguments": {
                    "id": poi_id
                }
            })

            print(f"POI详情结果: {result[:200]}...")

            # 解析结果并提取图片
            import json
            import re

            # 尝试从结果中提取JSON
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data

            return {"raw": result}

        except Exception as e:
            print(f"❌ 获取POI详情失败: {str(e)}")
            return {}


# 创建全局服务实例
_amap_service = None
_amap_service_lock = threading.Lock()


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service

    if _amap_service is None:
        # 加锁的原因：路由层现在通过 asyncio.to_thread(get_amap_service) 调用本函数，
        # 并发首次请求会同时进入这里。若不加锁，「检查 - 构造 - 赋值」之间存在窗口，
        # 多个线程会各建一个 AmapService（每次构造都包含一次 MCP 服务发现往返），
        # 后写入的覆盖先写入的，多出来的实例被直接丢弃。
        with _amap_service_lock:
            if _amap_service is None:
                _amap_service = AmapService()

    return _amap_service


def reset_amap_service() -> None:
    """重置高德地图服务与 MCP 工具实例（用于运行时配置更新后热生效）。"""
    global _amap_service, _amap_mcp_tool
    with _amap_service_lock:
        _amap_service = None
    with _amap_mcp_tool_lock:
        _amap_mcp_tool = None
