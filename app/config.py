"""全局配置：pydantic-settings 读取 .env，集中管理路径与模型参数。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/ 的上一级中的 app 包所在目录）
# 本文件位于 <project>/app/app/config.py，项目根 = parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """所有配置项均可被 .env 或环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- LLM（阿里云百炼 DashScope，OpenAI 兼容模式；变量名沿用历史命名）-----
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_model: str = "qwen3.7-plus"
    text_model: str = "qwen3.7-plus"

    # ----- 数据库 -----
    master_db_path: str = "app/data/master.db"
    checkpoint_db_path: str = "app/data/checkpoints.db"

    # ----- 业务路径 -----
    downstream_file_path: str = (
        "/Users/nz/Downloads/yamato/96/"
        "ContentsOfTheContainer_202624_青島XD_20260708.xlsx"
    )
    upstream_root: str = "/Users/nz/Downloads/yamato/96/工厂"
    output_dir: str = "app/output"
    alias_map_path: str = "app/alias_map.json"

    # ----- 提取引擎开关：1 = 强制 mock（冒烟测试用，不依赖真实 LLM）-----
    extraction_mock: bool = False

    # ----- 业务阈值 -----
    weight_diff_warn_ratio: float = 0.05  # 与历史单重差异超过 5% 标记 Warning
    fuzzy_match_score_cutoff: float = 40.0  # rapidfuzz 匹配最低分

    # ----- 下游装箱单关键列名（日本买家标准模板）-----
    col_factory: str = "MAKER_MEI_KJ"   # 工厂名（日文/英文）
    col_sku: str = "SHOHIN_CD"          # SKU 代码
    col_net: str = "净重"               # 待填：净重（原文件无此列，Node6 首次写入时添加）
    col_gross: str = "毛重"             # 待填：毛重（同上）
    col_name_cn: str = "中文品名"       # 待填：中文品名（同上，填主数据 name_cn）
    # 外箱发注数量：净重/毛重 = 单重 × 该列（2026-07-28 用户定，与 GT 聚合口径一致）
    col_qty: str = "SOTOBAKO_D_HACCHU_SU"

    def resolve(self, p: str) -> Path:
        """把相对路径解析为基于项目根的绝对路径。"""
        path = Path(p)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def master_db_abs(self) -> Path:
        return self.resolve(self.master_db_path)

    @property
    def checkpoint_db_abs(self) -> Path:
        return self.resolve(self.checkpoint_db_path)

    @property
    def output_dir_abs(self) -> Path:
        return self.resolve(self.output_dir)

    @property
    def alias_map_abs(self) -> Path:
        return self.resolve(self.alias_map_path)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # 确保数据/输出目录存在
    s.master_db_abs.parent.mkdir(parents=True, exist_ok=True)
    s.checkpoint_db_abs.parent.mkdir(parents=True, exist_ok=True)
    s.output_dir_abs.mkdir(parents=True, exist_ok=True)
    return s
