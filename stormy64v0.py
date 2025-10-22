# Stormy64 2.0 - SM64-inspired sandbox
# A SamSoft Production

from ursina import *
from random import uniform, choice
import math

APP_TITLE = "Stormy64 2.0 - SamSoft"
TARGET_SHARDS = 8

class ThirdPersonPlayer(Entity):
    def __init__(self, **kwargs):
        super().__init__()
        # Visual
        self.model = 'cube'
        self.color = color.azure
        self.scale = Vec3(0.8, 1.2, 0.8)
        self.origin_y = -0.5
        self.collider = 'box'

        # Movement / physics
        self.speed = 7
        self.jump_power = 7.2
        self.gravity = 22
        self.max_fall_speed = 40
        self.velocity_y = 0
        self.grounded = False
        self.height = 1.0

        # Camera pivot (orbiting)
        self.cam_pivot = Entity(parent=self, y=0.9)
        camera.parent = self.cam_pivot
        camera.position = (0, 1.5, -6)
        camera.rotation_x = 15
        mouse.locked = False

        # Settings
        self.turn_smoothing = 18
        self.move_block_probe = 0.5
        
        # Game state
        self.collected = 0
        self.alive = True

        for k,v in kwargs.items():
            setattr(self, k, v)

    def input(self, key):
        if key == 'space' and self.grounded and self.alive:
            self.velocity_y = self.jump_power
            self.grounded = False

    def _move_horizontal(self, move_vec):
        """Improved horizontal collision detection"""
        if move_vec.length() == 0:
            return
            
        dir_norm = move_vec.normalized()
        hit = raycast(self.world_position + Vec3(0,0.5,0), dir_norm,
                      distance=self.move_block_probe, ignore=[self], debug=False)
        if not hit.hit:
            self.position += move_vec
        else:
            # Try sliding along walls
            slide_dir = Vec3(dir_norm.z, 0, -dir_norm.x)  # perpendicular
            slide_hit = raycast(self.world_position + Vec3(0,0.5,0), slide_dir,
                              distance=self.move_block_probe, ignore=[self], debug=False)
            if not slide_hit.hit:
                self.position += slide_dir * move_vec.length() * 0.7

    def _apply_gravity(self):
        if not self.alive:
            return
            
        self.velocity_y = max(self.velocity_y - self.gravity * time.dt, -self.max_fall_speed)
        move_y = self.velocity_y * time.dt

        if move_y <= 0:
            # Falling: cast slightly longer to catch steps/slopes
            hit = raycast(self.world_position + Vec3(0,0.1,0), Vec3(0,-1,0),
                          distance=abs(move_y)+0.15, ignore=[self], debug=False)
            if hit.hit:
                self.y = hit.world_point.y + self.height/2
                self.velocity_y = 0
                self.grounded = True
                return
        else:
            # Going up: prevent ceiling clipping
            hit_up = raycast(self.world_position + Vec3(0,0.6,0), Vec3(0,1,0),
                             distance=move_y+0.05, ignore=[self], debug=False)
            if hit_up.hit:
                self.velocity_y = -0.1
                move_y = 0

        self.y += move_y
        self.grounded = False

    def update(self):
        if not self.alive:
            return
            
        # Camera orbit (hold RMB)
        if held_keys['right mouse']:
            self.cam_pivot.rotation_y += mouse.velocity[0] * 180
            camera.rotation_x = clamp(camera.rotation_x - mouse.velocity[1]*180, -10, 35)

        # Zoom
        camera.z = clamp(camera.z + mouse.wheel * 0.75, -12, -3)

        # Input vector (camera-relative)
        input_x = (held_keys['d'] - held_keys['a'])
        input_z = (held_keys['w'] - held_keys['s'])
        move = Vec3(0,0,0)
        if input_x or input_z:
            forward = self.cam_pivot.forward
            right = self.cam_pivot.right
            forward.y = 0; right.y = 0
            forward = forward.normalized()
            right = right.normalized()
            move = (forward * input_z + right * input_x)
            if move.length() > 0:
                move = move.normalized() * self.speed * time.dt

            # Face movement direction smoothly
            target_yaw = math.degrees(math.atan2(move.x, move.z))
            self.rotation_y = lerp(self.rotation_y, target_yaw, time.dt*self.turn_smoothing)

        # Horizontal then vertical
        self._move_horizontal(move)
        self._apply_gravity()
        
    def respawn(self, position):
        """Reset player to spawn position"""
        self.position = position
        self.velocity_y = 0
        self.alive = True
        self.color = color.azure


class Rotator(Entity):
    """For collectibles and star: rotates and gently bobs."""
    def __init__(self, bob=True, **kwargs):
        super().__init__(**kwargs)
        self._t = uniform(0, 2*math.pi)
        self._bob = bob
        self.initial_y = self.y
        
    def update(self):
        self.rotation_y += 90 * time.dt
        if self._bob:
            self._t += time.dt * 2
            self.y = self.initial_y + math.sin(self._t) * 0.3


class PatrolEnemy(Entity):
    def __init__(self, p0, p1, speed=2.0, **kwargs):
        super().__init__(**kwargs)
        self.model = 'sphere'
        self.color = color.red
        self.scale = 0.9
        self.collider = 'sphere'
        self.p0, self.p1 = Vec3(p0), Vec3(p1)
        self.u = 0.0
        self.dir = 1
        self.speed = speed
        self.shadow = Entity(model='circle', rotation_x=90, scale=0.9, color=color.black33, y=0.01, parent=self)

    def update(self):
        self.u += self.dir * self.speed * time.dt * 0.2
        if self.u > 1: 
            self.u, self.dir = 1, -1
        if self.u < 0: 
            self.u, self.dir = 0, 1
        self.position = lerp(self.p0, self.p1, self.u)
        self.rotation_y += 60 * time.dt


class HUD(Entity):
    def __init__(self):
        super().__init__(parent=camera.ui)
        self.shard_text = Text(text='Shards: 0/8', position=window.top_left, origin=(-.5,.5), scale=1.5, background=True)
        self.shard_text.bg.color = color.rgba(0,0,0,150)
        
        # Health indicator
        self.health_display = Entity(parent=camera.ui, model='quad', color=color.green, 
                                   scale=(0.3, 0.05), position=window.bottom_left + (0.2, 0.1))
        
    def set_count(self, n):
        self.shard_text.text = f' Shards: {n}/{TARGET_SHARDS} '
        
    def update_health(self, health):
        self.health_display.scale_x = 0.3 * (health / 100)
        self.health_display.color = color.green if health > 50 else color.yellow if health > 25 else color.red


class GameManager(Entity):
    def __init__(self):
        super().__init__()
        self.player = None
        self.hud = None
        self.enemies = []
        self.collectibles = []
        self.star_spawned = False
        self.game_active = True
        
    def spawn_star(self):
        """Spawn the goal star; touching it wins."""
        if self.star_spawned:
            return
            
        self.star_spawned = True
        star = Rotator(model='sphere', color=color.gold, scale=1.1, position=Vec3(0, 6.5, 6))
        star.glow = Entity(model='sphere', scale=1.6, color=color.rgba(255,220,60,60), parent=star)
        star_text = Text("Goal: Touch the Star!", origin=(0,0), position=(0,.4), scale=1.2, 
                        color=color.gold, world_parent=star)
        star_text.billboard = True
        star.collider = 'sphere'

        def check_win():
            if self.game_active and star.enabled and distance(star.world_position, self.player.world_position) < 2.0:
                destroy(star)
                self.show_win_screen()
            elif self.game_active and star.enabled:
                invoke(check_win, delay=0.02)
                
        check_win()

    def show_win_screen(self):
        self.game_active = False
        win_panel = Panel(scale=(.8,.4), color=color.rgba(0,0,0,200), parent=camera.ui)
        Text("VICTORY! 🎉", parent=win_panel, origin=(0,0), scale=2.5, y=.1, color=color.gold)
        Text(f"Collected {self.player.collected} shards", parent=win_panel, origin=(0,0), scale=1.5, y=-.05)
        Text("Press R to play again", parent=win_panel, origin=(0,0), scale=1.2, y=-.15, color=color.cyan)
        
    def show_game_over(self):
        self.game_active = False
        game_over_panel = Panel(scale=(.7,.3), color=color.rgba(0,0,0,200), parent=camera.ui)
        Text("GAME OVER", parent=game_over_panel, origin=(0,0), scale=2.5, y=.1, color=color.red)
        Text("Press R to restart", parent=game_over_panel, origin=(0,0), scale=1.2, y=-.08, color=color.cyan)


def build_level():
    """Enhanced battlefield with more varied terrain"""
    # Ground with texture-like coloring
    ground = Entity(model='plane', scale=60, texture='white_cube', 
                   texture_scale=(30, 30), color=color.lime.tint(-0.2), collider='box')
    
    Sky(texture='sky_sunset')  # More interesting sky

    # Hill (more natural looking)
    for i in range(6):
        scale_factor = 8 - i
        Entity(model='cube', color=color.rgb(100-i*5, 170-i*10, 80-i*5), 
               collider='box', position=(0, i*0.5, 6+i*0.5), 
               scale=(scale_factor, 0.5, scale_factor))

    # Bridge with supports
    for x in range(-4, 5):
        Entity(model='cube', color=color.rgb(120,120,120), collider='box',
               position=(x*1.2, 1.2, -2), scale=(1.1, 0.2, 3))
    
    # Bridge supports
    for x in [-5, 5]:
        Entity(model='cube', color=color.rgb(100,100,100), collider='box',
               position=(x*1.2, 0.6, -2), scale=(0.8, 1.2, 0.8))

    # Ramps with better positioning
    Entity(model='cube', color=color.rgb(90,160,90), collider='box',
           position=(-10, .5, 0), scale=(6, .5, 6), rotation_x=18)
    Entity(model='cube', color=color.rgb(90,160,90), collider='box',
           position=(10, .5, 3), scale=(6, .5, 6), rotation_x=-14)

    # Pillars with variation
    pillar_colors = [color.rgb(160,130,100), color.rgb(150,120,90), color.rgb(170,140,110)]
    for i, px in enumerate([-8,-6,-4, 4,6,8]):
        pillar_color = choice(pillar_colors)
        Entity(model='cube', color=pillar_color, collider='box',
               position=(px, 1.5, -10), scale=(1.2, 3, 1.2))

    # Enhanced arch
    for i in range(5):
        arch_color = color.rgb(150,150,180).tint(-i*0.05)
        Entity(model='cube', color=arch_color, collider='box',
               position=(0+i*1.1, 2.2+i*0.05, -11), scale=(1, .4, 1))
        Entity(model='cube', color=arch_color, collider='box',
               position=(0-i*1.1, 2.2+i*0.05, -11), scale=(1, .4, 1))
    
    # Additional platforms
    Entity(model='cube', color=color.orange, collider='box',
           position=(-12, 3, -5), scale=(3, 0.3, 3))
    Entity(model='cube', color=color.blue, collider='box',
           position=(12, 2, -8), scale=(2, 0.3, 2))


def make_collectibles(game_manager):
    """Scatter rotating shards with varied colors"""
    player = game_manager.player
    hud = game_manager.hud
    
    shards = []
    spots = [
        Vec3(-11, 2.2, 0), Vec3(9.5, 2.0, 4), Vec3(0, 3.2, 11),
        Vec3(4.8, 1.6, -2), Vec3(-6.5, 1.8, -2), Vec3(-2, 0.6, 7),
        Vec3(7, 0.7, -9), Vec3(-8, 2.3, -10), Vec3(0, 4.0, 12),
        Vec3(-12, 3.3, -5), Vec3(12, 2.3, -8)  # New platform shards
    ]
    
    shard_colors = [color.yellow, color.orange, color.cyan, color.violet, color.gold]
    
    def tick():
        if not game_manager.game_active:
            return
            
        for s in shards[:]:  # Use slice copy for safe removal
            if distance(s.world_position, player.world_position) < 1.5:
                shards.remove(s)
                destroy(s)
                player.collected += 1
                hud.set_count(player.collected)
                
                # Collection effect
                popup = Text("+1", position=s.world_position, world_space=True, 
                           scale=2, color=color.yellow)
                popup.animate_position(popup.position + (0, 3, 0), duration=1)
                destroy(popup, delay=1)
                
                if player.collected >= TARGET_SHARDS:
                    game_manager.spawn_star()
                    
        invoke(tick, delay=0.05)

    for i, p in enumerate(spots[:TARGET_SHARDS]):
        shard_color = shard_colors[i % len(shard_colors)]
        shard = Rotator(model='sphere', color=shard_color, scale=0.6, position=p)
        shard.glow = Entity(model='sphere', scale=1.3, color=color.rgba(*shard_color, 100), parent=shard)
        shard.shadow = Entity(model='circle', rotation_x=90, scale=0.8, color=color.black33, y=0.01, parent=shard)
        shards.append(shard)

    tick()
    return shards


def setup_enemies(game_manager):
    """Setup multiple patrol enemies"""
    player = game_manager.player
    enemies = []
    
    patrol_routes = [
        (Vec3(-6, 1.6, -2), Vec3(6, 1.6, -2)),  # Bridge
        (Vec3(-8, 0.5, 8), Vec3(8, 0.5, 8)),    # Behind hill
        (Vec3(-5, 3.3, -5), Vec3(-5, 3.3, 1)),  # High platform
    ]
    
    for i, (p0, p1) in enumerate(patrol_routes):
        enemy = PatrolEnemy(p0=p0, p1=p1, speed=1.5 + i*0.3)
        enemies.append(enemy)
        
        def make_enemy_tick(enemy):
            def enemy_tick():
                if (game_manager.game_active and player.alive and 
                    distance(enemy.world_position, player.world_position) < 1.5):
                    player.alive = False
                    player.color = color.gray
                    
                    # Respawn after delay
                    invoke(lambda: player.respawn(Vec3(0, 3, 0)), delay=2)
                    invoke(game_manager.show_game_over, delay=1)
                    
                if game_manager.game_active:
                    invoke(enemy_tick, delay=0.1)
            return enemy_tick
            
        make_enemy_tick(enemy)()
    
    return enemies


def main():
    app = Ursina(title=APP_TITLE, borderless=False)
    window.color = color.rgb(135, 206, 235)
    window.fps_counter.enabled = True
    window.exit_button.visible = False
    
    # Improved lighting
    AmbientLight(color=color.rgba(200, 200, 255, 100))
    DirectionalLight(y=10, z=5, shadows=True, rotation=(45, -45, 45))
    
    # Build level
    build_level()
    
    # Initialize game manager
    game_manager = GameManager()
    
    # Create player
    player = ThirdPersonPlayer(position=(0, 3, 0))
    game_manager.player = player
    
    # Create HUD
    hud = HUD()
    game_manager.hud = hud
    
    # Setup enemies and collectibles
    game_manager.enemies = setup_enemies(game_manager)
    game_manager.collectibles = make_collectibles(game_manager)
    
    # Global input handler
    def input(key):
        if key == 'escape':
            application.quit()
        if key == 'r':  # Restart game
            scene.clear()
            destroy(game_manager)
            main()
        if key == 'p':  # Pause
            application.paused = not application.paused
    
    # Instructions
    instruction = Text("WASD: Move | Space: Jump | RMB: Look | R: Restart", 
                      position=(0, -0.45), parent=camera.ui, scale=1.2, 
                      background=True, color=color.white)
    instruction.bg.color = color.rgba(0, 0, 0, 150)
    
    destroy(instruction, delay=5)
    
    app.run()

if __name__ == '__main__':
    main()
