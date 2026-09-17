# -*- coding: utf-8 -*-
"""
机器人项目配置管理器
所有配置项均以属性默认值为准，无需外部 JSON 文件。
用法：
    from 配置 import Config
    cfg = Config()
    print(cfg.stop_heart)  # 访问属性
"""
import os


class Config:
    """配置管理类——直接使用代码内默认值，无需外部配置文件"""
    
    def __init__(self):
        pass
    
    def load(self):
        """无需加载外部文件，默认值已在各属性 property 中定义"""
        pass
    
    def save(self):
        """无需保存，所有配置直接修改 配置.py 中的默认值即可"""
        pass
    
    # ========== 游戏与程序路径 ==========
    
    @property
    def tesseract_exe(self):
        """Tesseract OCR 引擎路径（相对或绝对路径均可）"""
        return getattr(self, "_tesseract_exe", "src/lib/tesseract/tesseract.exe")
    
    @tesseract_exe.setter
    def tesseract_exe(self, value):
        self._tesseract_exe = str(value)
    
    @property
    def forager_exe(self):
        """Forager 游戏主程序路径"""
        return getattr(self, "_forager_exe", "C:\\Games\\Forager\\Forager.exe")
    
    @forager_exe.setter
    def forager_exe(self, value):
        self._forager_exe = str(value)
    
    @property
    def sound_effect(self):
        """提示音效文件路径（可选，无声音则设为空字符串""）"""
        return getattr(self, "_sound_effect", "src/resources/音效.mp3")
    
    @sound_effect.setter
    def sound_effect(self, value):
        self._sound_effect = str(value) if value else ""
    
    # ========== 识别目标设置 ==========
    
    @property
    def targets(self):
        """需要点击的目标列表（如 ["终结 - 亮"]）"""
        return getattr(self, "_targets", ["终结 - 亮"])
    
    @targets.setter
    def targets(self, value):
        self._targets = list(value)
    
    @property
    def negatives(self):
        """要忽略的干扰项列表（长得像但不是目标的文字）"""
        return getattr(self, "_negatives", ["毁灭 - 亮"])
    
    @negatives.setter
    def negatives(self, value):
        self._negatives = list(value)
    
    # ========== 核心识别参数 ==========
    
    @property
    def rounds(self):
        """每轮每个区域要悬停识别的次数（推荐 2-4 次）"""
        return int(getattr(self, "_rounds", 2))
    
    @rounds.setter
    def rounds(self, value):
        self._rounds = max(1, int(value))
    
    @property
    def match_threshold(self):
        """匹配相似度阈值（0~1），超过才认为是目标"""
        return float(getattr(self, "_match_threshold", 0.55))
    
    @match_threshold.setter
    def match_threshold(self, value):
        self._match_threshold = max(0, min(1, float(value)))
    
    @property
    def negative_margin(self):
        """目标需领先干扰项的最低百分差（0~1），防止误判"""
        return float(getattr(self, "_negative_margin", 0.18))
    
    @negative_margin.setter
    def negative_margin(self, value):
        self._negative_margin = max(0, min(1, float(value)))
    
    # ========== 停止条件 ==========
    
    @property
    def stop_heart(self):
        """爱心数降到几颗时自动停止（建议 1-3 颗）"""
        return max(1, int(getattr(self, "_stop_heart", 3)))
    
    @stop_heart.setter
    def stop_heart(self, value):
        self._stop_heart = max(1, int(value))
    
    # ========== 图标识别区域 ==========
    
    @property
    def regions(self):
        """鼠标悬停位置 + 点击区域列表 [{name, left, top, right, bottom}]"""
        return getattr(self, "_regions", [])
    
    @regions.setter
    def regions(self, value):
        self._regions = list(value)
    
    @property
    def ocr_regions(self):
        """OCR 文字识别框列表（用于从截图读字）"""
        return getattr(self, "_ocr_regions", [])
    
    @ocr_regions.setter
    def ocr_regions(self, value):
        self._ocr_regions = list(value)
    
    # ========== 便捷方法 ==========
    
    def get_region_by_name(self, name):
        """根据名称查找区域配置"""
        for r in self.regions:
            if isinstance(r, dict) and r.get("name") == name:
                return r
        return None
    
    def get_ocr_region_by_name(self, name):
        """根据名称查找 OCR 区域配置"""
        for r in self.ocr_regions:
            if isinstance(r, dict) and r.get("name") == name:
                return r
        return None


def main():
    """测试配置文件是否正确加载"""
    cfg = Config()
    print("=" * 50)
    print("配置文件加载成功！")
    print("=" * 50)
    print(f"\n游戏路径：{cfg.forager_exe}")
    print(f"OCR 引擎：{cfg.tesseract_exe}")
    print(f"音效文件：{cfg.sound_effect or '无'}")
    print(f"\n识别目标：{cfg.targets}")
    print(f"干扰项：{cfg.negatives}")
    print(f"\n识别次数：{cfg.rounds}")
    print(f"匹配阈值：{cfg.match_threshold*100:.0f}%")
    print(f"干扰容忍度：{cfg.negative_margin*100:.0f}%")
    print(f"\n停止条件：剩余 {cfg.stop_heart} 颗爱心")
    print(f"\n图标区域数：{len(cfg.regions)}")
    print(f"OCR 区域数：{len(cfg.ocr_regions)}")


if __name__ == "__main__":
    main()
