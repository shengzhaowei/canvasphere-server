"""Seed 9 built-in curated Studio workflow skills across Pecan Studio categories."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UserSkill, new_prefixed_id, utcnow

BUILTIN_SKILLS_CATALOG: list[dict[str, str]] = [
    {
        "name": "script-beat-sheet",
        "display_name": "三幕剧节拍与分场表",
        "category": "剧本与故事",
        "description": "将一句话创意拆解为三幕结构、情绪曲线、场次清单与核心台词锚点。",
        "content": "# 三幕剧节拍与分场表\n1. 先确定主角目标、阻力与转折点。\n2. 输出 6–12 个结构化场次，每场标明时间、场景、冲突与视觉钩子。\n3. 避免空泛形容词，台词控制在短促、有潜台词的电影对白节奏。",
    },
    {
        "name": "storyboard",
        "display_name": "电影感分镜脚本",
        "category": "分镜与摄影",
        "description": "按镜头编号输出景别、机位运动、构图焦段、光影与转场逻辑。",
        "content": "# 电影感分镜脚本\n按 Shot 01..N 列出：景别（远/全/中/近/特写）、焦段（24mm/35mm/85mm）、运镜（推/拉/摇/移/跟/升降）、主体调度与剪辑接点。",
    },
    {
        "name": "image-prompt",
        "display_name": "视觉概念提示词工程",
        "category": "画面与风格",
        "description": "将中文创意转化为高可控的电影光影、材质、构图与色调图像提示词。",
        "content": "# 视觉概念提示词工程\n结构：主体动作 + 环境空间 + 构图焦段 + 主辅光与体积光 + 胶片质感与色彩控制。明确负面约束，确保角色外貌与服装一致。",
    },
    {
        "name": "anamorphic-lighting",
        "display_name": "宽银幕光影与色彩基调",
        "category": "画面与风格",
        "description": "建立 2.39:1 宽银幕电影布光方案、冷暖对比色卡与镜头眩光风格规范。",
        "content": "# 宽银幕光影与色彩基调\n定义主色/辅助色/点缀色比例（60/30/10），明确关键光方向、轮廓光与空气透视层次，保持整片统一色温。",
    },
    {
        "name": "video-camera-language",
        "display_name": "动态镜头运动导演",
        "category": "视频创作",
        "description": "针对首尾帧与图生视频任务设计物理合理的运镜轨迹与节奏控制。",
        "content": "# 动态镜头运动导演\n将 5–10 秒镜头拆分为起始定格、中段位移与收尾落幅，明确主体运动速度与镜头跟随关系，避免多轴复杂抖动导致画面崩坏。",
    },
    {
        "name": "action-vfx-choreography",
        "display_name": "动作调度与特效分镜",
        "category": "动作与特效",
        "description": "设计打斗、追逐、粒子与流体特效的起承转合和关键打击帧。",
        "content": "# 动作调度与特效分镜\n明确蓄力帧、爆发瞬间与余波扩散三段节奏；描述物理反馈（烟尘、火花、光晕、镜头微震）与慢动作切点。",
    },
    {
        "name": "sound-mood-design",
        "display_name": "声音氛围与配乐设计",
        "category": "声音与配乐",
        "description": "规划对白音色情绪、环境拟音分层与 Suno 配乐 BPM/配器提示词。",
        "content": "# 声音氛围与配乐设计\n按「对白层 + 环境底噪 + 动作拟音 + 音乐情绪」四轨规划；标明音乐 BPM、调性、主奏乐器与情绪高潮时间点。",
    },
    {
        "name": "grid-split-director",
        "display_name": "四宫格/九宫格分镜拆图流",
        "category": "素材与后期",
        "description": "生成统一角色与画风的多格分镜总图，并配合 grid.split 自动拆分为独立镜头节点。",
        "content": "# 四宫格/九宫格分镜拆图流\n要求单张总图内部无文字水印、等宽网格排布、人物造型与光影严格一致，便于生成后直接执行 grid.split 拆为独立镜头节点并串联视频。",
    },
    {
        "name": "worldbuilding-research",
        "display_name": "世界观设定与视觉考据",
        "category": "研究与辅助",
        "description": "系统梳理时代背景、建筑尺度、道具材质、服饰符号与视觉参考清单。",
        "content": "# 世界观设定与视觉考据\n从地理气候、建筑形态、交通与科技层级、服饰纹理四个维度建立项目圣经文档，供橘子与小a直接引用。",
    },
]


def ensure_builtin_skills_seeded(db: Session) -> int:
    """Idempotently seed curated built-in skills (is_builtin=True, owner_user_id=None)."""
    existing_names = set(
        db.scalars(select(UserSkill.name).where(UserSkill.is_builtin.is_(True))).all()
    )
    now = utcnow()
    inserted = 0
    for item in BUILTIN_SKILLS_CATALOG:
        if item["name"] in existing_names:
            continue
        db.add(
            UserSkill(
                id=new_prefixed_id("skl"),
                owner_user_id=None,
                name=item["name"],
                display_name=item["display_name"],
                description=item["description"],
                category=item["category"],
                content=item["content"],
                frontmatter_json={
                    "name": item["name"],
                    "display_name": item["display_name"],
                    "category": item["category"],
                    "builtin": "true",
                },
                files_json=[{"path": f"{item['name']}/SKILL.md", "size_bytes": len(item["content"].encode("utf-8"))}],
                is_builtin=True,
                created_at=now,
                updated_at=now,
            )
        )
        inserted += 1
    if inserted:
        db.commit()
    return inserted
