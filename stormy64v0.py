#!/usr/bin/env python3
"""
Stormy64 2.0 — Peach's Castle courtyard (SM64 DS layout, PC-port movement)

Single file only (no extra assets)  •  pip install ursina
Python 3.10+ recommended.
"""
from __future__ import annotations

import math
import sys
import traceback
from random import uniform

MIN_PYTHON = (3, 10)   # changed from 3.14 to 3.10 for wider compatibility
APP_TITLE = "Stormy64 2.0 — Peach's Castle (SM64 DS)"
TARGET_RED_COINS = 8
MAX_HEALTH = 8

def _require_python() -> None:
    if sys.version_info < MIN_PYTHON:
        v = ".".join(map(str, sys.version_info[:3]))
        need = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        print(f"Stormy64 needs Python {need}+ (this interpreter is {v}).")
        sys.exit(1)

_require_python()

try:
    from ursina import *
except ImportError as exc:
    print("Missing Ursina. Install with:  pip install ursina")
    raise SystemExit(1) from exc

# --- SM64-ish tuning (scaled for Ursina courtyard units @ 60 FPS) ---
SPAWN_POS = Vec3(0, 1.5, 11)
SPAWN_GRACE_SEC = 2.5
ENEMY_HIT_RADIUS = 1.05
HIT_DAMAGE = 2
INVULN_SEC = 1.6

# SM64 PC-port style speeds (decomp walk 32 / run 48 → scaled for courtyard)
WALK_MAX = 8.5
RUN_MAX = 15.0
GROUND_ACCEL = 46.0
GROUND_DECEL = 58.0
AIR_ACCEL = 14.0
AIR_DECEL = 10.0
GRAVITY = 30.0
GRAVITY_HOLD_MULT = 0.52
MAX_FALL = 42.0
JUMP_V = (8.0, 9.2, 10.4)
LONG_JUMP_H = 14.0
LONG_JUMP_V = 7.8
TRIPLE_JUMP_WINDOW = 0.36
DIVE_SPEED = 16.0

# Mario + SM64 DS courtyard palette
MARIO_RED = color.rgb32(255, 40, 40)
MARIO_BLUE = color.rgb32(40, 60, 220)
MARIO_SKIN = color.rgb32(255, 190, 150)
MARIO_HAIR = color.rgb32(120, 70, 20)
DS_BRICK = color.rgb32(130, 95, 75)
DS_PAVEMENT = color.rgb32(215, 215, 225)
DS_GRASS = color.rgb32(65, 145, 55)
DS_DIRT = color.rgb32(110, 85, 55)
DS_TREE = color.rgb32(45, 120, 40)
DS_CASTLE = color.rgb32(235, 200, 215)
DS_CASTLE_TRIM = color.rgb32(255, 170, 190)
DS_CASTLE_STONE = color.rgb32(248, 228, 238)
DS_CASTLE_ROOF = color.rgb32(215, 55, 95)
DS_CASTLE_WINDOW = color.rgb32(90, 170, 255)
DS_FOUNTAIN_STONE = color.rgb32(170, 175, 185)
MENU_ITEMS = [
    "Play Game",
    "Help",
    "Sound Settings",
    "About",
    "Exit Game",
]
UI_MENU_BG = color.rgba(0.08, 0.05, 0.12, 0.82)
UI_MENU_TITLE = color.rgb32(255, 220, 100)
UI_MENU_TEXT = color.rgb32(255, 240, 250)
UI_MENU_SELECT = color.rgb32(255, 255, 120)
UI_MENU_DIM = color.rgb32(180, 150, 170)
DS_WATER = color.rgb32(80, 160, 240)
RED_COIN = color.rgb32(255, 45, 45)
STAR_GOLD = color.rgb32(255, 220, 60)
BOO_WHITE = color.rgb32(230, 230, 255)
COURTYARD_HALF = 14


def _flat_dir(v: Vec3) -> Vec3:
    flat = Vec3(v.x, 0, v.z)
    if flat.length() < 1e-6:
        return Vec3(0, 0, 1)
    return flat.normalized()


def _color_alpha(c, alpha: float):
    if hasattr(c, "r"):
        r, g, b = float(c.r), float(c.g), float(c.b)
        if r > 1 or g > 1 or b > 1:
            r, g, b = r / 255.0, g / 255.0, b / 255.0
        return color.rgba(r, g, b, alpha)
    return color.rgba(1, 1, 1, alpha)


def _key_axis(pos: str, neg: str) -> float:
    return float(bool(held_keys.get(pos, False))) - float(bool(held_keys.get(neg, False)))


def _safe_destroy(entity) -> None:
    if entity is None:
        return
    try:
        destroy(entity)
    except Exception:
        pass


def _build_mario_visual(parent: Entity) -> None:
    """Simple SM64-colored Mario stack (no external models)."""
    Entity(parent=parent, model="cube", color=MARIO_BLUE, scale=(0.42, 0.5, 0.32), y=0.28, z=-0.02)
    Entity(parent=parent, model="cube", color=MARIO_RED, scale=(0.48, 0.45, 0.38), y=0.72)
    Entity(parent=parent, model="sphere", color=MARIO_SKIN, scale=0.38, y=1.08)
    Entity(parent=parent, model="cube", color=MARIO_RED, scale=(0.46, 0.18, 0.46), y=1.38)
    Entity(parent=parent, model="cube", color=MARIO_HAIR, scale=(0.12, 0.08, 0.12), y=1.48, z=0.2)


class Mario64Player(Entity):
    """SM64 PC-port inspired: accel walk/run, triple jump, long jump, Lakitu cam."""

    def __init__(self, **kwargs):
        super().__init__(collider="sphere", scale=0.55, visible=False, **kwargs)
        self.height = 1.35
        self.visual = Entity(parent=self)
        _build_mario_visual(self.visual)

        self.vel_h = Vec3(0, 0, 0)
        self.velocity_y = 0.0
        self.grounded = False
        self.face_yaw = 0.0
        self.jump_chain = 0
        self.time_since_jump = 99.0
        self.jump_held = False
        self.long_jump_timer = 0.0
        self.invuln_timer = 0.0
        self._dive_used = False

        self.collected = 0
        self.alive = True
        self.health = MAX_HEALTH
        self._zoom_delta = 0.0

        self.cam_yaw = 180.0
        self.cam_pitch = 18.0
        self.cam_dist = 7.5
        self.cam_height = 2.8
        self.cam_pivot = Entity(parent=scene)
        camera.parent = scene
        mouse.locked = False

    def _running(self) -> bool:
        return bool(held_keys.get("shift", False) or held_keys.get("left shift", False))

    def _stick(self) -> Vec3:
        x = _key_axis("d", "a")
        z = _key_axis("w", "s")
        if abs(x) < 0.01 and abs(z) < 0.01:
            return Vec3(0, 0, 0)
        forward = _flat_dir(Vec3(math.sin(math.radians(self.cam_yaw)), 0, math.cos(math.radians(self.cam_yaw))))
        right = Vec3(forward.z, 0, -forward.x)
        move = forward * z + right * x
        return move.normalized() if move.length() > 0 else Vec3(0, 0, 0)

    def try_dive(self) -> None:
        """SM64 PC-port dive: forward slam while airborne (once per airtime)."""
        if not self.alive or self.grounded or self._dive_used:
            return
        self._dive_used = True
        yaw_r = math.radians(self.face_yaw)
        fwd = Vec3(math.sin(yaw_r), 0, math.cos(yaw_r))
        self.vel_h = fwd * DIVE_SPEED
        self.velocity_y = min(self.velocity_y, -5.0)

    def jump(self) -> None:
        if not self.alive:
            return
        running = self._running()
        stick = self._stick()

        if self.grounded:
            if running and stick.length() > 0.1 and held_keys.get("c", False):
                self.long_jump_timer = 0.35
                self.velocity_y = LONG_JUMP_V
                self.grounded = False
                dir_m = stick
                self.vel_h = Vec3(dir_m.x * LONG_JUMP_H, 0, dir_m.z * LONG_JUMP_H)
                self.face_yaw = math.degrees(math.atan2(dir_m.x, dir_m.z))
                self.jump_chain = 0
                return

            if self.time_since_jump < TRIPLE_JUMP_WINDOW:
                self.jump_chain = min(3, self.jump_chain + 1)
            else:
                self.jump_chain = 1
            idx = self.jump_chain - 1
            self.velocity_y = JUMP_V[idx]
            self.grounded = False
            self.time_since_jump = 0.0
        elif self.jump_chain < 3 and self.time_since_jump < TRIPLE_JUMP_WINDOW:
            self.jump_chain += 1
            idx = min(2, self.jump_chain - 1)
            self.velocity_y = max(self.velocity_y, JUMP_V[idx] * 0.92)

    def input(self, key):
        if key == "space":
            self.jump_held = True
            self.jump()
        if key == "x" and not self.grounded:
            self.try_dive()
        if key in ("scroll up",):
            self._zoom_delta += 1.0
        elif key in ("scroll down",):
            self._zoom_delta -= 1.0

    def _apply_horizontal_physics(self, wish_dir: Vec3, dt: float) -> None:
        max_spd = RUN_MAX if self._running() else WALK_MAX
        if self.long_jump_timer > 0:
            max_spd = LONG_JUMP_H

        target = wish_dir * max_spd if wish_dir.length() > 0 else Vec3(0, 0, 0)
        accel = GROUND_ACCEL if self.grounded else AIR_ACCEL
        decel = GROUND_DECEL if self.grounded else AIR_DECEL

        for axis in (0, 2):
            i = axis
            cur = self.vel_h[i]
            tgt = target[i]
            if abs(tgt) > 0.01:
                if cur < tgt:
                    cur = min(tgt, cur + accel * dt)
                else:
                    cur = max(tgt, cur - accel * dt)
            else:
                if cur > 0:
                    cur = max(0, cur - decel * dt)
                elif cur < 0:
                    cur = min(0, cur + decel * dt)
            self.vel_h[i] = cur

        if wish_dir.length() > 0 and self.long_jump_timer <= 0:
            self.face_yaw = math.degrees(math.atan2(self.vel_h.x, self.vel_h.z))

        move = Vec3(self.vel_h.x * dt, 0, self.vel_h.z * dt)
        if move.length() > 0:
            origin = self.world_position + Vec3(0, 0.55, 0)
            hit = raycast(origin, move.normalized(), distance=move.length() + 0.35, ignore=(self, self.visual), debug=False)
            if not hit.hit:
                self.position += move
            elif move.length() > 0.05:
                slide = Vec3(move.z, 0, -move.x)
                sh = raycast(origin, slide.normalized(), distance=slide.length() + 0.2, ignore=(self, self.visual), debug=False)
                if not sh.hit:
                    self.position += slide

        self.visual.rotation_y = self.face_yaw

    def _apply_gravity(self, dt: float) -> None:
        grav = GRAVITY
        if self.velocity_y > 0 and self.jump_held and not held_keys.get("space", False):
            self.jump_held = False
        if self.velocity_y > 0 and self.jump_held:
            grav *= GRAVITY_HOLD_MULT

        self.velocity_y = max(self.velocity_y - grav * dt, -MAX_FALL)
        move_y = self.velocity_y * dt

        if move_y <= 0:
            hit = raycast(
                self.world_position + Vec3(0, 0.15, 0),
                Vec3(0, -1, 0),
                distance=abs(move_y) + 0.2,
                ignore=(self, self.visual),
                debug=False,
            )
            if hit.hit:
                self.y = hit.world_point.y + self.height * 0.5
                self.velocity_y = 0
                self.grounded = True
                self._dive_used = False
                if self.long_jump_timer <= 0:
                    self.vel_h *= 0.82
                return
        else:
            hit_up = raycast(
                self.world_position + Vec3(0, 0.55, 0),
                Vec3(0, 1, 0),
                distance=move_y + 0.08,
                ignore=(self, self.visual),
                debug=False,
            )
            if hit_up.hit:
                self.velocity_y = 0

        self.y += move_y
        self.grounded = False

    def _update_camera(self, dt: float) -> None:
        if held_keys.get("right mouse", False) or held_keys.get("right mouse down", False):
            self.cam_yaw -= mouse.velocity[0] * 220
            self.cam_pitch = clamp(self.cam_pitch - mouse.velocity[1] * 180, 5, 55)
        wheel = getattr(mouse, "wheel", 0)
        if wheel:
            self._zoom_delta += wheel
        if self._zoom_delta:
            self.cam_dist = clamp(self.cam_dist + self._zoom_delta * 0.45, 4.5, 14)
            self._zoom_delta = 0.0

        target_p = self.world_position + Vec3(0, self.cam_height, 0)
        yaw_r = math.radians(self.cam_yaw)
        pitch_r = math.radians(self.cam_pitch)
        offset = Vec3(
            math.sin(yaw_r) * math.cos(pitch_r) * self.cam_dist,
            math.sin(pitch_r) * self.cam_dist,
            math.cos(yaw_r) * math.cos(pitch_r) * self.cam_dist,
        )
        cam_pos = target_p - offset
        self.cam_pivot.position = lerp(self.cam_pivot.position, target_p, dt * 8)
        camera.position = lerp(camera.position, cam_pos, dt * 10)
        camera.look_at(target_p + Vec3(0, 0.4, 0))

    def update(self):
        if not self.alive:
            return

        dt = time.dt
        self.time_since_jump += dt
        if self.long_jump_timer > 0:
            self.long_jump_timer = max(0, self.long_jump_timer - dt)
        if self.invuln_timer > 0:
            self.invuln_timer = max(0, self.invuln_timer - dt)
            self.visual.visible = int(time.time() * 12) % 2 == 0
        else:
            self.visual.visible = True

        wish = self._stick()
        self._apply_horizontal_physics(wish, dt)
        self._apply_gravity(dt)
        self._update_camera(dt)

    def take_damage(self, amount: int) -> bool:
        if self.invuln_timer > 0 or not self.alive:
            return False
        self.health = max(0, self.health - amount)
        self.invuln_timer = INVULN_SEC
        if self.health <= 0:
            self.alive = False
            self.visual.visible = False
            return True
        return False

    def respawn(self, position: Vec3) -> None:
        self.position = position
        self.vel_h = Vec3(0, 0, 0)
        self.velocity_y = 0
        self.alive = True
        self.health = MAX_HEALTH
        self.grounded = False
        self.invuln_timer = INVULN_SEC
        self._dive_used = False
        self.visual.visible = True


class Rotator(Entity):
    def __init__(self, bob=True, **kwargs):
        super().__init__(**kwargs)
        self._t = uniform(0, 2 * math.pi)
        self._bob = bob
        self.initial_y = self.y

    def update(self):
        self.rotation_y += 90 * time.dt
        if self._bob:
            self._t += time.dt * 2
            self.y = self.initial_y + math.sin(self._t) * 0.3


class Boo(Entity):
    """DS courtyard Boos — patrol the brick walls."""

    def __init__(self, p0, p1, speed=2.0, **kwargs):
        super().__init__(model="sphere", color=BOO_WHITE, scale=0.9, collider="sphere", **kwargs)
        self.p0, self.p1 = Vec3(p0), Vec3(p1)
        self.u = 0.0
        self.dir = 1
        self.speed = speed
        self.position = self.p0
        Entity(model="sphere", color=color.black, scale=(0.12, 0.16, 0.08), position=(-0.18, 0.12, 0.35), parent=self)
        Entity(model="sphere", color=color.black, scale=(0.12, 0.16, 0.08), position=(0.18, 0.12, 0.35), parent=self)
        Entity(model="circle", rotation_x=90, scale=0.9, color=color.black33, y=0.01, parent=self)

    def update(self):
        self.u += self.dir * self.speed * time.dt * 0.16
        if self.u > 1:
            self.u, self.dir = 1, -1
        if self.u < 0:
            self.u, self.dir = 0, 1
        self.position = lerp(self.p0, self.p1, self.u)
        self.y = self.p0.y + math.sin(time.time() * 2.2 + self.u * 4) * 0.35
        self.rotation_y += 40 * time.dt


class HUD(Entity):
    def __init__(self):
        super().__init__(parent=camera.ui)
        self.coin_text = Text(
            text=f"Red Coins 0/{TARGET_RED_COINS}",
            position=(-0.85, 0.45),
            origin=(-0.5, 0.5),
            scale=1.4,
            background=True,
        )
        self.coin_text.create_background(color=color.black66)
        self.health_root = Entity(parent=camera.ui, position=(-0.82, 0.38))
        self._hp_pips = []
        for i in range(MAX_HEALTH):
            pip = Entity(
                parent=self.health_root,
                model="quad",
                color=color.rgb32(255, 60, 60),
                scale=(0.035, 0.05),
                position=(i * 0.04, 0),
            )
            self._hp_pips.append(pip)

    def set_coins(self, n: int) -> None:
        self.coin_text.text = f" Red Coins {n}/{TARGET_RED_COINS} "

    def set_health(self, hp: int) -> None:
        for i, pip in enumerate(self._hp_pips):
            pip.enabled = i < hp


class GameManager(Entity):
    def __init__(self):
        super().__init__()
        self.player: Mario64Player | None = None
        self.hud: HUD | None = None
        self.enemies = []
        self.collectibles = []
        self.star_entity = None
        self.star_spawned = False
        self.game_active = True
        self.overlay = None
        self.spawn_grace = SPAWN_GRACE_SEC

    def _clear_overlay(self):
        _safe_destroy(self.overlay)
        self.overlay = None

    def spawn_star(self) -> None:
        if self.star_spawned:
            return
        self.star_spawned = True
        star = Rotator(model="sphere", color=STAR_GOLD, scale=1.2, position=Vec3(0, 5.8, -5.5))
        Entity(model="sphere", scale=1.7, color=color.rgba(1, 0.86, 0.24, 0.3), parent=star)
        Text(text="Power Star", parent=star, y=1.5, scale=9, color=STAR_GOLD, billboard=True)
        star.collider = "sphere"
        self.star_entity = star

    def on_star_touch(self) -> None:
        if not self.game_active:
            return
        self.game_active = False
        _safe_destroy(self.star_entity)
        self.star_entity = None
        self.show_win_screen()

    def show_win_screen(self) -> None:
        self._clear_overlay()
        self.game_active = False
        self.overlay = Panel(scale=(0.85, 0.42), color=color.black66, parent=camera.ui)
        Text("COURSE CLEAR!", parent=self.overlay, origin=(0, 0), scale=2.2, y=0.12, color=STAR_GOLD)
        Text(
            f"You got a Power Star! ({getattr(self.player, 'collected', 0)} red coins)",
            parent=self.overlay,
            origin=(0, 0),
            scale=1.3,
            y=-0.02,
        )
        Text("Press R to play again", parent=self.overlay, origin=(0, 0), scale=1.1, y=-0.14, color=color.cyan)

    def show_game_over(self) -> None:
        self._clear_overlay()
        self.game_active = False
        self.overlay = Panel(scale=(0.7, 0.3), color=color.black66, parent=camera.ui)
        Text("GAME OVER", parent=self.overlay, origin=(0, 0), scale=2.5, y=0.1, color=color.red)
        Text("Press R to restart", parent=self.overlay, origin=(0, 0), scale=1.2, y=-0.08, color=color.cyan)

    def on_player_hit(self, enemy=None) -> None:
        if not self.game_active or not self.player or self.spawn_grace > 0:
            return
        if enemy is not None:
            push = self.player.world_position - enemy.world_position
            push = _flat_dir(push) * 4.5
            self.player.position += Vec3(push.x, 0, push.z)
        if self.player.take_damage(HIT_DAMAGE):
            if self.hud:
                self.hud.set_health(0)
            self.show_game_over()
        elif self.hud:
            self.hud.set_health(self.player.health)

    def update(self):
        if not self.game_active or not self.player:
            return

        if self.spawn_grace > 0:
            self.spawn_grace = max(0.0, self.spawn_grace - time.dt)

        player = self.player

        for coin in list(self.collectibles):
            try:
                if coin is None or not coin.enabled:
                    continue
                if distance(coin.world_position, player.world_position) < 1.25:
                    pos = coin.world_position
                    self.collectibles.remove(coin)
                    _safe_destroy(coin)
                    player.collected += 1
                    if self.hud:
                        self.hud.set_coins(player.collected)
                    if player.collected >= TARGET_RED_COINS:
                        self.spawn_star()
            except Exception:
                if coin in self.collectibles:
                    self.collectibles.remove(coin)

        if self.star_entity:
            try:
                if self.star_entity.enabled and distance(self.star_entity.world_position, player.world_position) < 2.2:
                    self.on_star_touch()
            except Exception:
                self.star_entity = None

        for enemy in self.enemies:
            try:
                if enemy and enemy.enabled and player.alive and player.invuln_timer <= 0:
                    if distance(enemy.world_position, player.world_position) < ENEMY_HIT_RADIUS:
                        self.on_player_hit(enemy)
                        break
            except Exception:
                continue


class SoundSettings:
    music_on = True
    sfx_on = True
    music_volume = 0.75


sound_settings = SoundSettings()


class MainMenu(Entity):
    """SM64 DS–style title menu (single-file UI)."""

    def __init__(self):
        super().__init__()
        self.active = True
        self.state = "main"
        self.index = 0
        self.sound_row = 0
        self._ui: list = []

    def _clear_ui(self) -> None:
        for ent in self._ui:
            _safe_destroy(ent)
        self._ui.clear()

    def _add(self, ent) -> None:
        self._ui.append(ent)
        return ent

    def show(self) -> None:
        self.active = True
        self.state = "main"
        self.index = 0
        self._rebuild()

    def hide(self) -> None:
        self.active = False
        self._clear_ui()

    def _rebuild(self) -> None:
        self._clear_ui()
        self._add(Panel(parent=camera.ui, scale=(1.6, 1.0), color=UI_MENU_BG, z=-1))
        self._add(
            Text(
                "Stormy64 2.0",
                parent=camera.ui,
                y=0.38,
                scale=2.6,
                origin=(0, 0),
                color=UI_MENU_TITLE,
            )
        )
        self._add(
            Text(
                "Peach's Castle — Super Mario 64 DS",
                parent=camera.ui,
                y=0.30,
                scale=1.15,
                origin=(0, 0),
                color=UI_MENU_DIM,
            )
        )

        if self.state == "main":
            self._build_main_rows()
        elif self.state == "help":
            self._build_help()
        elif self.state == "sound":
            self._build_sound()
        elif self.state == "about":
            self._build_about()

        hint = "UP/DOWN select  •  ENTER confirm  •  ESC back"
        if self.state == "main" and self.index == len(MENU_ITEMS) - 1:
            hint = "UP/DOWN select  •  ENTER or ESC to quit"
        self._add(Text(hint, parent=camera.ui, y=-0.42, scale=0.95, origin=(0, 0), color=UI_MENU_DIM))

    def _build_main_rows(self) -> None:
        for i, label in enumerate(MENU_ITEMS):
            col = UI_MENU_SELECT if i == self.index else UI_MENU_TEXT
            mark = "> " if i == self.index else "  "
            self._add(
                Text(
                    mark + label,
                    parent=camera.ui,
                    y=0.14 - i * 0.09,
                    scale=1.45,
                    origin=(0, 0),
                    color=col,
                )
            )

    def _build_help(self) -> None:
        lines = [
            "GOAL: Collect 8 red coins, then touch the Power Star.",
            "",
            "WASD — move",
            "Shift — run",
            "Space — jump (triple jump chain)",
            "X — dive (in air)",
            "C + run + jump — long jump",
            "Right mouse — rotate camera",
            "Scroll — zoom",
            "R — restart course",
            "ESC — return to main menu",
        ]
        for i, line in enumerate(lines):
            self._add(
                Text(line, parent=camera.ui, y=0.22 - i * 0.055, scale=1.05, origin=(0, 0), color=UI_MENU_TEXT)
            )

    def _build_sound(self) -> None:
        rows = [
            ("Music", "music_on", None),
            ("Sound effects", "sfx_on", None),
            ("Music volume", "music_volume", "pct"),
        ]
        for i, (label, attr, mode) in enumerate(rows):
            col = UI_MENU_SELECT if i == self.sound_row else UI_MENU_TEXT
            mark = "> " if i == self.sound_row else "  "
            val = getattr(sound_settings, attr)
            if mode == "pct":
                text = f"{mark}{label}: {int(val * 100)}%"
            else:
                text = f"{mark}{label}: {'ON' if val else 'OFF'}"
            self._add(Text(text, parent=camera.ui, y=0.16 - i * 0.1, scale=1.35, origin=(0, 0), color=col))
        self._add(
            Text(
                "LEFT/RIGHT change value  •  ENTER toggles",
                parent=camera.ui,
                y=-0.08,
                scale=0.95,
                origin=(0, 0),
                color=UI_MENU_DIM,
            )
        )

    def _build_about(self) -> None:
        lines = [
            "Stormy64 2.0 — SamSoft",
            "A single-file tribute to",
            "Super Mario 64 DS Peach's Castle hub.",
            "",
            "Movement inspired by the SM64 PC port.",
            "No Nintendo assets — procedural geometry only.",
            "",
            "Python + Ursina",
        ]
        for i, line in enumerate(lines):
            self._add(
                Text(line, parent=camera.ui, y=0.2 - i * 0.06, scale=1.1, origin=(0, 0), color=UI_MENU_TEXT)
            )

    def handle_key(self, key: str) -> None:
        if not self.active:
            return
        if self.state == "main":
            self._main_input(key)
        elif self.state == "sound":
            self._sound_input(key)
        else:
            if key in ("escape", "enter", "space"):
                self.state = "main"
                self._rebuild()

    def _main_input(self, key: str) -> None:
        if key in ("up arrow", "w"):
            self.index = (self.index - 1) % len(MENU_ITEMS)
            self._rebuild()
        elif key in ("down arrow", "s"):
            self.index = (self.index + 1) % len(MENU_ITEMS)
            self._rebuild()
        elif key in ("enter", "space"):
            choice = MENU_ITEMS[self.index]
            if choice == "Play Game":
                session.start_game()
            elif choice == "Help":
                self.state = "help"
                self._rebuild()
            elif choice == "Sound Settings":
                self.state = "sound"
                self.sound_row = 0
                self._rebuild()
            elif choice == "About":
                self.state = "about"
                self._rebuild()
            elif choice == "Exit Game":
                application.quit()
        elif key == "escape" and self.index == len(MENU_ITEMS) - 1:
            application.quit()

    def _sound_input(self, key: str) -> None:
        attrs = ["music_on", "sfx_on", "music_volume"]
        if key in ("up arrow", "w"):
            self.sound_row = (self.sound_row - 1) % 3
            self._rebuild()
        elif key in ("down arrow", "s"):
            self.sound_row = (self.sound_row + 1) % 3
            self._rebuild()
        elif key in ("enter", "space"):
            attr = attrs[self.sound_row]
            if attr == "music_volume":
                sound_settings.music_volume = 0.25 if sound_settings.music_volume > 0.5 else 0.75
            else:
                setattr(sound_settings, attr, not getattr(sound_settings, attr))
            self._rebuild()
        elif key == "left arrow":
            if self.sound_row == 2:
                sound_settings.music_volume = max(0.0, sound_settings.music_volume - 0.1)
                self._rebuild()
        elif key == "right arrow":
            if self.sound_row == 2:
                sound_settings.music_volume = min(1.0, sound_settings.music_volume + 0.1)
                self._rebuild()
        elif key == "escape":
            self.state = "main"
            self._rebuild()


class SessionController(Entity):
    def input(self, key):
        if session.menu and session.menu.active:
            session.menu.handle_key(key)
            return
        if key == "escape":
            session.return_to_menu()
            return
        if key == "p":
            application.paused = not application.paused
        if key == "r":
            session.restart()

    def update(self):
        if session.in_game and session.game_manager and session.game_manager.player:
            if not held_keys.get("space", False):
                session.game_manager.player.jump_held = False


class Session:
    def __init__(self):
        self.started = False
        self.in_game = False
        self.game_manager: GameManager | None = None
        self.menu: MainMenu | None = None
        self.instruction = None
        self.controller = None
        self._level_roots: list = []
        self._menu_roots: list = []
        self._lights: list = []

    def restart(self):
        if self.in_game:
            self.bootstrap()

    def return_to_menu(self):
        self.teardown_game()
        self.show_menu()

    def show_menu(self):
        self.teardown_menu()
        self.teardown_game()
        self.in_game = False
        window.color = color.rgb32(100, 150, 220)
        self._menu_roots = build_menu_backdrop()
        self._lights = _setup_lights()
        camera.parent = scene
        camera.position = (8, 7, 22)
        camera.look_at(Vec3(0, 6, -10))
        self.menu = MainMenu()
        self.menu.show()
        if not self.controller:
            self.controller = SessionController()

    def start_game(self):
        if self.menu:
            self.menu.hide()
            self.menu = None
        self.teardown_menu()
        self.bootstrap()

    def teardown_menu(self):
        for root in list(self._menu_roots):
            _safe_destroy(root)
        self._menu_roots.clear()
        if self.menu:
            self.menu.hide()
            self.menu = None

    def teardown_game(self):
        if self.game_manager:
            self.game_manager._clear_overlay()
            _safe_destroy(self.game_manager.star_entity)
            for ent in list(self.game_manager.collectibles):
                _safe_destroy(ent)
            for ent in list(self.game_manager.enemies):
                _safe_destroy(ent)
            player = self.game_manager.player
            if player:
                _safe_destroy(getattr(player, "cam_pivot", None))
                _safe_destroy(player)
        camera.parent = scene
        camera.position = (0, 0, 0)
        camera.rotation = (0, 0, 0)
        mouse.locked = False
        _safe_destroy(self.controller)
        _safe_destroy(self.game_manager)
        _safe_destroy(self.instruction)
        for root in list(self._level_roots):
            _safe_destroy(root)
        self._level_roots.clear()
        for light in list(self._lights):
            _safe_destroy(light)
        self._lights.clear()
        for child in list(camera.ui.children):
            _safe_destroy(child)
        self.game_manager = None
        self.instruction = None
        self.in_game = False

    def teardown(self):
        self.teardown_game()
        self.teardown_menu()
        for light in list(self._lights):
            _safe_destroy(light)
        self._lights.clear()
        _safe_destroy(self.controller)
        self.controller = None

    def bootstrap(self):
        self.teardown_game()
        self.started = True
        self.in_game = True
        window.color = color.rgb32(120, 185, 255)
        self._level_roots = build_level()
        self.game_manager = GameManager()
        self.game_manager.spawn_grace = SPAWN_GRACE_SEC
        player = Mario64Player(position=SPAWN_POS)
        self.game_manager.player = player
        hud = HUD()
        hud.set_health(player.health)
        self.game_manager.hud = hud
        self.game_manager.enemies = setup_enemies()
        self.game_manager.collectibles = make_red_coins()
        self.instruction = Text(
            "SM64 DS: WASD move | Shift run | Space jump | X dive | C+run+jump long jump | RMB cam | R restart",
            position=(0, -0.46),
            parent=camera.ui,
            scale=1.05,
            background=True,
            color=color.white,
        )
        self.instruction.create_background(color=color.black66)
        destroy(self.instruction, delay=8)
        self.controller = SessionController()
        self._lights = _setup_lights()


session = Session()


def _track(roots: list, entity):
    roots.append(entity)
    return entity


def _level_box(roots, pos, scale, col, *, collider="box"):
    kw = dict(model="cube", color=col, position=pos, scale=scale)
    if collider is not None:
        kw["collider"] = collider
    return _track(roots, Entity(**kw))


def _ds_tree(roots, x: float, z: float) -> None:
    _track(roots, Entity(model="cylinder", color=DS_DIRT, scale=(2.4, 0.12, 2.4), position=(x, 0.06, z), collider="box"))
    _track(roots, Entity(model="cylinder", color=color.rgb32(85, 55, 30), scale=(0.4, 1.3, 0.4), position=(x, 0.75, z)))
    _track(roots, Entity(model="sphere", color=DS_TREE, scale=(1.9, 2.3, 1.9), position=(x, 2.35, z)))


def _ds_signpost(roots, x: float, z: float) -> None:
    _track(roots, Entity(model="cube", color=color.rgb32(90, 60, 35), scale=(0.15, 1.1, 0.15), position=(x, 0.55, z)))
    _track(roots, Entity(model="quad", color=color.rgb32(240, 230, 200), scale=(0.7, 0.45), position=(x, 1.25, z), rotation_y=45))


def _ds_star_fountain(roots) -> None:
    _level_box(roots, (0, 0.22, 0), (6.2, 0.45, 6.2), DS_FOUNTAIN_STONE)
    _track(roots, Entity(model="cylinder", color=DS_WATER, scale=(3.8, 0.55, 3.8), position=(0, 0.55, 0)))
    _track(roots, Entity(model="cylinder", color=DS_FOUNTAIN_STONE, scale=(0.9, 2.2, 0.9), position=(0, 1.35, 0)))
    star_statue = _track(roots, Rotator(model="sphere", color=STAR_GOLD, scale=0.55, position=(0, 2.85, 0), bob=False))
    Entity(model="sphere", scale=1.1, color=_color_alpha(STAR_GOLD, 0.22), parent=star_statue)


def _ds_castle_north(roots, h: float) -> None:
    z = -h + 0.65
    _level_box(roots, (0, 4.2, z), (20, 8.5, 2.6), DS_CASTLE)
    _level_box(roots, (-6.5, 4.8, z + 0.4), (6, 1.2, 2), DS_CASTLE_TRIM, collider=None)
    _level_box(roots, (6.5, 4.8, z + 0.4), (6, 1.2, 2), DS_CASTLE_TRIM, collider=None)
    _level_box(roots, (-3.2, 2.8, z + 0.9), (1.4, 5.6, 1.6), DS_CASTLE_TRIM)
    _level_box(roots, (3.2, 2.8, z + 0.9), (1.4, 5.6, 1.6), DS_CASTLE_TRIM)
    for i in range(3):
        _level_box(roots, (0, 5.8 + i * 0.4, z + 0.5), (8 - i * 1.6, 0.35, 1.4), DS_CASTLE_TRIM, collider=None)


def _ds_battle_fort(roots, h: float) -> None:
    bx = h - 3.5
    for dz, dy in ((0, 0), (2.2, 0.6), (-2.2, 0.6)):
        _level_box(roots, (bx, 1.0 + dy, dz), (2.8, 2.0 + dy * 2, 2.8), DS_BRICK)


def build_level():
    roots = []
    h = COURTYARD_HALF
    _track(
        roots,
        Entity(
            model="plane",
            scale=40,
            texture="white_cube",
            texture_scale=(20, 20),
            color=DS_GRASS,
            collider="box",
            y=0,
        ),
    )
    _track(
        roots,
        Entity(
            model="plane",
            scale=18,
            texture="white_cube",
            texture_scale=(9, 9),
            color=DS_PAVEMENT,
            y=0.02,
            collider=None,
        ),
    )
    try:
        _track(roots, Sky(texture="sky_default"))
    except Exception:
        _track(roots, Sky())

    wall_h, thick = 4.8, 1.35
    _level_box(roots, (0, wall_h * 0.5, h), (h * 2 + thick * 2, wall_h, thick), DS_BRICK)
    _level_box(roots, (0, wall_h * 0.5, -h), (h * 2 + thick * 2, wall_h, thick), DS_BRICK)
    _level_box(roots, (h, wall_h * 0.5, 0), (thick, wall_h, h * 2), DS_BRICK)
    _level_box(roots, (-h, wall_h * 0.5, 0), (thick, wall_h, h * 2), DS_BRICK)

    for pos, scale in (
        ((0, wall_h + 0.3, h), (h * 2 + 3, 0.45, 1.4)),
        ((0, wall_h + 0.3, -h), (h * 2 + 3, 0.45, 1.4)),
        ((h, wall_h + 0.3, 0), (1.4, 0.45, h * 2)),
        ((-h, wall_h + 0.3, 0), (1.4, 0.45, h * 2)),
    ):
        _level_box(roots, pos, scale, color.rgb32(100, 75, 58), collider=None)

    _ds_castle_north(roots, h)
    _ds_battle_fort(roots, h)
    _ds_star_fountain(roots)

    for tx, tz in ((-5.5, -5.5), (5.5, -5.5), (-5.5, 5.5), (5.5, 5.5)):
        _ds_tree(roots, tx, tz)

    for sx, sz in ((-7, 7), (7, 7), (-7, -7), (7, -7)):
        _ds_signpost(roots, sx, sz)

    gate_z = h - 0.5
    _level_box(roots, (-5.5, 2.6, gate_z), (1.6, 5.2, 1.6), DS_CASTLE_TRIM)
    _level_box(roots, (5.5, 2.6, gate_z), (1.6, 5.2, 1.6), DS_CASTLE_TRIM)
    for i in range(3):
        _level_box(roots, (0, 4.4 + i * 0.35, gate_z + 0.5), (8 - i * 1.5, 0.35, 1.2), DS_CASTLE_TRIM, collider=None)

    return roots


def make_red_coins():
    spots = [
        Vec3(-5.5, 1.4, -5.5),
        Vec3(5.5, 1.4, -5.5),
        Vec3(-5.5, 1.4, 5.5),
        Vec3(5.5, 1.4, 5.5),
        Vec3(3.2, 1.5, 0.5),
        Vec3(-3.2, 1.5, -0.5),
        Vec3(0, 1.6, 9),
        Vec3(-11, 1.3, 0),
    ]
    coins = []
    for p in spots[:TARGET_RED_COINS]:
        coin = Rotator(model="sphere", color=RED_COIN, scale=0.55, position=p, bob=True)
        Entity(model="sphere", scale=1.15, color=_color_alpha(RED_COIN, 0.25), parent=coin)
        coins.append(coin)
    return coins


def setup_enemies():
    h = COURTYARD_HALF - 2.5
    # Patrol walls only — no south-wall route through spawn (0, _, 11).
    routes = [
        (Vec3(-h, 1.4, -h + 1), Vec3(-h, 1.4, h - 3)),
        (Vec3(h, 1.4, -h + 1), Vec3(h, 1.4, h - 3)),
        (Vec3(-h + 1, 1.4, -h), Vec3(h - 1, 1.4, -h)),
    ]
    return [Boo(p0=p0, p1=p1, speed=1.35 + i * 0.2) for i, (p0, p1) in enumerate(routes)]


def _setup_lights():
    lights = []
    try:
        lights.append(AmbientLight(color=color.rgba(0.72, 0.74, 0.88, 0.45)))
        lights.append(DirectionalLight(y=12, z=8, shadows=False, rotation=(50, -40, 45)))
    except Exception:
        pass
    return lights


def main() -> None:
    print(f"Stormy64 SM64 | Python {sys.version.split()[0]}")
    app = Ursina(title=APP_TITLE, borderless=False)
    window.color = color.rgb32(120, 185, 255)
    window.fps_counter.enabled = True
    window.exit_button.visible = False
    if hasattr(window, "editor_ui"):
        window.editor_ui.enabled = False
    session.bootstrap()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("Press Enter to close...")
