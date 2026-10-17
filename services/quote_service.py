"""Motivational quote engine, coaching personas, and form cue service.

Zero AI: 100% curated, high-impact quotes and deterministic coaching logic.
Supports multiple coach personas:
- 'beast': Goggins / Arnold / Jocko (Hardcore discipline, raw intensity)
- 'stoic': Marcus Aurelius / Seneca / Epictetus / Musashi (Mental mastery, calm focus)
- 'system': Solo Leveling / Hunter quests (The System rewards dedication)
- 'athlete': Bruce Lee / Calisthenics masters / Kobe Bryant (Technique and craft)
"""

import random
from typing import Dict, List, Optional, Tuple

PERSONAS = {
    "beast": {
        "name": "Beast Mode",
        "icon": "🦍",
        "description": "Hardcore, relentless discipline (Goggins, Arnold, Jocko)",
    },
    "stoic": {
        "name": "Stoic Master",
        "icon": "🏛",
        "description": "Inner fortress, calm discipline (Marcus Aurelius, Seneca)",
    },
    "system": {
        "name": "The System",
        "icon": "⚔️",
        "description": "Solo Leveling Quest engine & Hunter power upgrades",
    },
    "athlete": {
        "name": "Elite Athlete",
        "icon": "🥊",
        "description": "Calisthenics mastery, sharp focus & technique",
    },
}

MORNING_QUOTES: Dict[str, List[str]] = {
    "beast": [
        "“Stay hard. The pain you endure today becomes your armor tomorrow.” — David Goggins",
        "“The last 3 or 4 reps is what makes the muscle grow. This area of pain divides a champion from someone who is not.” — Arnold Schwarzenegger",
        "“Discipline equals freedom. Wake up, attack the day, no excuses.” — Jocko Willink",
        "“Don't stop when you're tired. Stop when you're done.”",
        "“You have to build calluses on your mind just like on your hands.” — David Goggins",
    ],
    "stoic": [
        "“No man is more unhappy than he who never faces adversity. For he is not permitted to prove himself.” — Seneca",
        "“You have power over your mind — not outside events. Realize this, and you will find strength.” — Marcus Aurelius",
        "“First say to yourself what you would be; and then do what you have to do.” — Epictetus",
        "“Today I will do what others won't, so tomorrow I can accomplish what others cannot.”",
        "“Step by step, day by day. Perfection is built through steady habits.” — Miyamoto Musashi",
    ],
    "system": [
        "« [SYSTEM NOTIFICATION] Daily Quest available. Train to surpass your human limits. »",
        "« A true Hunter never ignores the daily training. Your stats are waiting to rise. »",
        "« [QUEST OBJECTIVE] Conquer today's volume. Survival demands strength. »",
        "« The difference between an E-Rank and an S-Rank is daily non-negotiable execution. »",
    ],
    "athlete": [
        "“I fear not the man who has practiced 10,000 kicks once, but I fear the man who has practiced one kick 10,000 times.” — Bruce Lee",
        "“Rest at the end, not in the middle.” — Kobe Bryant",
        "“Master the fundamentals. The body weight you command is the strength you own.”",
        "“Consistency creates mastery. Lock in, execute each rep with clean form.”",
    ],
}

IN_WORKOUT_HYPE: Dict[str, Dict[str, List[str]]] = {
    "first_blood": {
        "beast": [
            "🔥 First blood! You broke the inertia. Keep moving forward!",
            "💥 Set 1 in the books. The friction is broken—stay locked in!",
            "🦍 Good start! Don't let your mind negotiate now. Attack the next set!",
        ],
        "stoic": [
            "🏛 Well begun is half done. Move forward with steady focus.",
            "🛡️ You stepped across the line. Let your actions speak.",
        ],
        "system": [
            "⚔️ [QUEST UPDATE] Initial rep recorded. The System acknowledges your start.",
            "⚡ Momentum activated. Experience is accumulating.",
        ],
        "athlete": [
            "🥊 Clean opening reps. Stay tight, control the tempo.",
            "🎯 First set locked. Find your breathing rhythm now.",
        ],
    },
    "halfway": {
        "beast": [
            "⚡ 50% crushed! This is where champions separate from the pack. DIG IN!",
            "🔥 Halfway home! The easy part is behind you—now the real workout begins!",
            "💪 You're at the summit. Now sprint down the other side!",
        ],
        "stoic": [
            "🏛 Halfway mark reached. The mind is steady; finish what you started.",
            "⚖️ Equilibrium reached. Complete the remaining half with the same dignity.",
        ],
        "system": [
            "⚔️ [PROGRESS 50%] Threshold surpassed. Hunter's stamina holding strong.",
            "⚡ Quest halfway complete. Power level rising.",
        ],
        "athlete": [
            "🥊 50% done! Keep your core braced and don't rush the tempo.",
            "🎯 Halfway through. Smooth reps, full range of motion.",
        ],
    },
    "overdelivered": {
        "beast": [
            "🚀 OVERDELIVERED! That extra rep is where new muscle and mental grit are forged!",
            "👑 Target exceeded! You didn't just meet the bar—you raised it!",
            "💥 Smashed past the goal! That's the mindset of a savage!",
        ],
        "stoic": [
            "🏛 You went beyond duty. Excellence is found in what is given freely.",
            "🌟 Beyond the target. You mastered yourself today.",
        ],
        "system": [
            "⚔️ [BONUS CLEAR] Rep quota exceeded! Bonus EXP credited to your hunter ledger.",
            "🏆 Overachiever bonus unlocked. Physical stats incremented.",
        ],
        "athlete": [
            "🥊 Crushed the target with extra in the tank! Outstanding capacity.",
            "🎯 Target blown away. Superb work capacity today.",
        ],
    },
    "victory": {
        "beast": [
            "🏆 WORKOUT DESTROYED! Another day you didn't quit. Proud of you!",
            "🔥 Victory is yours! You paid the toll in sweat. Rest up and reload!",
            "👑 That is how champions are made. Outstanding execution!",
        ],
        "stoic": [
            "🏛 Daily duty fulfilled. A day of discipline is a day well lived.",
            "🌿 Mission accomplished. Rest peacefully knowing you did your part.",
        ],
        "system": [
            "⚔️ [DAILY QUEST COMPLETE] Full quota secured! Streak intact and rank advancing.",
            "🎉 All objectives cleared. You have grown stronger today.",
        ],
        "athlete": [
            "🥊 Complete! Beautiful consistency. High-grade athletic work today.",
            "🎯 All movements finished with quality. Recovery starts now.",
        ],
    },
}

FORM_CUES: Dict[str, List[str]] = {
    "push-ups": [
        "📐 Elbows tucked at ~45° (never flared at 90°).",
        "🛡️ Lock your core and glutes into a rigid plank.",
        "⬇️ Full range: chest grazes floor, full extension at top.",
    ],
    "squats": [
        "🦶 Feet shoulder-width apart, knees track over toes.",
        "🍑 Sit hips back first, break parallel (thighs at/below 90°).",
        "Chest tall, push through midfoot and heels.",
    ],
    "sit-ups": [
        "🛡️ Curl your spine up vertebra by vertebra (no jerking neck).",
        "💨 Exhale sharply on the way up, inhale on the descent.",
        "Keep heels firmly planted on the floor.",
    ],
    "plank": [
        "🔒 Tuck your pelvis under (posterior pelvic tilt) to engage deep abs.",
        "Push elbows into floor, spread your shoulder blades apart.",
        "Breathe slow and steady — do not hold your breath.",
    ],
    "pull-ups": [
        "🧗 Start from a dead hang, engage lats before pulling.",
        "Lead with chest to bar, elbows drive down towards pockets.",
        "Controlled descent — don't drop abruptly on elbows.",
    ],
    "dips": [
        "📐 Slight forward lean targets chest; upright targets triceps.",
        "Lower until upper arms are parallel with bars (~90°).",
        "Keep shoulders depressed (away from ears) throughout.",
    ],
}


def get_morning_quote(persona: str = "beast") -> str:
    """Returns an inspiring quote for the morning workout push."""
    quotes = MORNING_QUOTES.get(persona, MORNING_QUOTES["beast"])
    return random.choice(quotes)


def get_in_workout_quote(
    event: str,
    persona: str = "beast",
) -> str:
    """Returns a contextual hype message for in-workout events.
    Events: 'first_blood', 'halfway', 'overdelivered', 'victory'
    """
    category = IN_WORKOUT_HYPE.get(event, IN_WORKOUT_HYPE["first_blood"])
    quotes = category.get(persona, category.get("beast", ["Keep pushing!"]))
    return random.choice(quotes)


def get_form_cues(exercise_name: str) -> Optional[List[str]]:
    """Returns 2-3 expert technique cues for an exercise."""
    key = exercise_name.lower().replace(" ", "").replace("_", "").replace("-", "")
    for name, cues in FORM_CUES.items():
        norm_name = name.lower().replace(" ", "").replace("_", "").replace("-", "")
        if norm_name in key or key in norm_name:
            return cues
    return None


def get_smart_pacing(exercise_name: str, target_reps: int) -> Optional[str]:
    """Suggests an optimal set/rep and rest breakdown for large targets."""
    if target_reps <= 20:
        return None
    if "plank" in exercise_name.lower() or target_reps >= 180:
        return None

    # Deterministic set splitting
    if target_reps <= 40:
        return f"💡 Pacing: 2 sets of {target_reps // 2} reps · 60s rest"
    elif target_reps <= 75:
        per_set = target_reps // 3
        rem = target_reps % 3
        dist = f"{per_set} / {per_set} / {per_set + rem}"
        return f"💡 Pacing: 3 sets ({dist}) · 90s rest"
    elif target_reps <= 120:
        per_set = target_reps // 3
        rem = target_reps % 3
        dist = f"{per_set} / {per_set} / {per_set + rem}"
        return f"💡 Pacing: 3 sets of ~{per_set} reps ({dist}) · 90s rest"
    else:
        per_set = target_reps // 4
        rem = target_reps % 4
        return f"💡 Pacing: 4 sets of ~{per_set} reps · 90s–120s rest"
