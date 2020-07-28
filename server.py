import aiohttp.web
import asyncio
import collections
import itertools
import json
import math
import random
import ssl
import sys
import time


MAX_MOVE_SPEED = 0.1  # "units" (ill-defined for now)
TAIL_LIFESPAN = 1  # seconds
SECONDS_PER_UPDATE = 0.1
MIN_FOODS = 10
PLAYER_COLORS = [
  (0.8, 0.0, 0.0),
  (0.5, 0.0, 0.0),
  (0.8, 0.5, 0.0),
  (0.0, 0.8, 0.0),
  (0.0, 0.5, 0.0),
  (0.0, 0.8, 0.8),
  (0.0, 0.5, 0.5),
  (0.0, 0.5, 0.8),
  (0.8, 0.0, 0.8),
  (0.5, 0.0, 0.5),
]


# derived constants
MAX_MOVE_SPEED2 = MAX_MOVE_SPEED * MAX_MOVE_SPEED


class Vertex:
  def __init__(self, x, y, t=None):
    self.x = x
    self.y = y
    self.t = t

  def get_state_json(self):
    return [self.t, self.x, self.y]


def dist2(x1, y1, x2, y2):
  xd = x1 - x2
  yd = y1 - y2
  return (xd * xd) + (yd * yd)


# def intersection(x1, y1, x2, y2, x3, y3, x4, y4):
#   # https://en.wikipedia.org/wiki/Line%E2%80%93line_intersection
#   denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
#   if denom == 0:
#     return None  # lines are parallel or degenerate
#   t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
#   u = ((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
#   if (t < 0) || (u < 0) || (t > 1) || (u > 1):
#     return None


def intersection(x1, y1, x2, y2, x3, y3, x4, y4):
  # TODO: there appears to be a bug here where it doesn't always find the intersection :(
  # https://en.wikipedia.org/wiki/Line%E2%80%93line_intersection
  denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
  if denom == 0:
    return None  # lines are parallel or degenerate
  x1y2_det = x1 * y2 - y1 * x2
  x3y4_det = x3 * y4 - y3 * x4
  px = (x1y2_det * (x3 - x4) - (x1 - x2) * x3y4_det) / denom
  py = (x1y2_det * (y3 - y4) - (y1 - y2) * x3y4_det) / denom

  # the above computation assumes infinitely long lines (so it can give a point
  # not actually on the segments); to fix this, just check if the point is
  # within the bounding box for both lines
  if (px < min(x1, x2)) or (px > max(x1, x2)) or (px < min(x3, x4)) or (px > max(x3, x4)):
    return None
  if (py < min(y1, y2)) or (py > max(y1, y2)) or (py < min(y3, y4)) or (py > max(y3, y4)):
    return None

  return (px, py)


def angle_to(x1, y1, x2, y2):
  return math.atan2(y2 - y1, x2 - x1)


def angle_diff(current_angle, new_angle):
  while current_angle < new_angle - math.pi:
    current_angle += 2 * math.pi
  while new_angle < current_angle - math.pi:
    new_angle += 2 * math.pi
  return new_angle - current_angle


def is_in_polygon(poly_vertices, x, y):
  # basic idea: add the difference between all the angles to the vertices
  # together. if (x, y) is outside the polygon, these will cancel out, leaving
  # us with a number close to zero (not exactly due to floating-point error).
  # if (x, y) is inside the polygon, they will not cancel out; the accumulated
  # angle will be close to 2*pi or -2*pi.
  accumulated_angle = 0

  current_angle = angle_to(x, y, poly_vertices[-1].x, poly_vertices[-1].y)
  for v in poly_vertices:
    new_angle = angle_to(x, y, v.x, v.y)
    diff = angle_diff(current_angle, new_angle)
    accumulated_angle += diff
    current_angle = new_angle

  return abs(accumulated_angle) > 0.1


def polygon_center(vertices):
  xs = sum(v.x for v in vertices)
  ys = sum(v.y for v in vertices)
  count = len(vertices)
  return Vertex(xs / count, ys / count)


class PlayerState:
  def __init__(self, name, x, y, r, color, ws):
    self.name = name
    self.x = x
    self.y = y
    self.r = r
    self.color = color
    self.ws = ws
    self.tail_points = collections.deque()
    self.score = 0
    self.invincibility_end_time = None

  def get_state_json(self):
    return {
      'x': self.x,
      'y': self.y,
      'r': self.r,
      'tail_points': [p.get_state_json() for p in self.tail_points],
      'score': self.score,
      'color': self.color,
      'invincible': self.invincibility_end_time is not None,
    }

  def update(self):
    now = time.time()

    if self.invincibility_end_time is not None and self.invincibility_end_time <= now:
      self.invincibility_end_time = None

    # TODO: use a slice instead of pop(0)'ing multiple times
    while len(self.tail_points) > 0 and self.tail_points[-1].t < now - TAIL_LIFESPAN:
      self.tail_points.pop()

  def on_move(self, x, y):
    now = time.time()
    if dist2(x, y, self.x, self.y) > MAX_MOVE_SPEED2:
      self.tail_points.clear()
    else:
      self.tail_points.appendleft(Vertex(self.x, self.y, now))

    self.x = x
    self.y = y

    if len(self.tail_points) <= 2:
      return None

    # if this move crossed another part of the tail, detect the loop and clear the tail
    # TODO: there are probably unaccounted-for cases here. for example, is it
    # possible to make more than one polygon in a single move?
    poly_vertices = None
    prev_point = None
    for i, tail_point in enumerate(self.tail_points):
      if prev_point is not None:
        isx = intersection(
            self.tail_points[0].x, self.tail_points[0].y,
            self.x, self.y,
            prev_point.x, prev_point.y,
            tail_point.x, tail_point.y)

        # if a loop is found, the polygon is formed from the intersection point
        # and all points of the tail except the current one
        if isx is not None:
          isx_tail_point = Vertex(isx[0], isx[1], now)
          poly_vertices = list(itertools.islice(self.tail_points, i))
          poly_vertices.append(isx_tail_point)
          self.tail_points.clear()
          self.tail_points.appendleft(isx_tail_point)
          break

      prev_point = tail_point

    return poly_vertices


class FoodState:
  def __init__(self, x, y, r, dx, dy, color):
    self.x = x
    self.y = y
    self.r = r
    self.dx = dx
    self.dy = dy
    self.color = color
    self.last_update_time = time.time()

  def get_state_json(self):
    return {
      'x': self.x,
      'y': self.y,
      'r': self.r,
      'dx': self.dx,
      'dy': self.dy,
      'color': self.color,
    }

  def update(self):
    now = time.time()
    delta = now - self.last_update_time
    self.last_update_time = now

    self.x += self.dx * delta
    self.y += self.dy * delta

    # bounce off walls
    if self.x <= 0:
      self.x = 0.0
      self.dx = -self.dx
    if self.y <= 0:
      self.y = 0.0
      self.dy = -self.dy
    if self.x >= 1:
      self.x = 1.0
      self.dx = -self.dx
    if self.y >= 1:
      self.y = 1.0
      self.dy = -self.dy


class Event:
  def __init__(self, x, y, score, player_name):
    self.x = x
    self.y = y
    self.score = score
    self.player_name = player_name

  def get_state_json(self):
    return {'x': self.x, 'y': self.y, 'score': self.score, 'player_name': self.player_name}


class GameState:
  def __init__(self, loop):
    self.name_to_player = {}
    self.id_to_food = {}
    self.watchers = set()
    self.next_food_id = 0
    self.events = []
    self.loop = loop
    self.loop.call_later(SECONDS_PER_UPDATE, self.schedule_update)

  def add_watcher(self, ws):
    self.watchers.add(ws);

  def remove_watcher(self, ws):
    self.watchers.remove(ws);

  def add_player(self, name, x, y, ws):
    if name in self.name_to_player:
      raise RuntimeError('player already exists')

    # choose a player color that conflicts with the fewest others
    color_to_count = {color: 0 for color in PLAYER_COLORS}
    for player in self.name_to_player.values():
      color_to_count[player.color] += 1
    min_count = min(color_to_count.values())
    candidate_colors = [color for color, count in color_to_count.items() if count == min_count]
    color = random.choice(candidate_colors)

    self.name_to_player[name] = PlayerState(name, x, y, 0.015, color, ws)

  def remove_player(self, name):
    del self.name_to_player[name]

  def player_exists(self, name):
    return name in self.name_to_player

  def create_food(self):
    food_id = self.next_food_id
    self.next_food_id += 1

    x, y = self.find_spawn_location()
    vel = random.random() * 0.02 + 0.01
    angle = random.random() * math.tau
    dx = math.sin(angle) * vel
    dy = math.cos(angle) * vel
    color = (0.3, 0.3, 0.3)

    self.id_to_food[food_id] = FoodState(x, y, 0.01, dx, dy, color)

  def find_spawn_location(self):
    # TODO: we should find a place far away from other players probably
    # for now we just use a random location
    return (random.random(), random.random())

  def on_player_move(self, player_name, x, y):
    player = self.name_to_player[player_name]
    capture_polygon = player.on_move(x, y)
    if capture_polygon is not None:
      event_score = 0
      captured_food_ids = []
      for food_id, food in self.id_to_food.items():
        if is_in_polygon(capture_polygon, food.x, food.y):
          event_score += 1
          captured_food_ids.append(food_id)

      for food_id in captured_food_ids:
        del self.id_to_food[food_id]

      for other_player_name, other_player in self.name_to_player.items():
        if other_player is player:
          continue
        if other_player.invincibility_end_time is not None:
          continue
        if is_in_polygon(capture_polygon, other_player.x, other_player.y):
          event_score += 1
          other_player_new_score = other_player.score // 2
          self.events.append(Event(other_player.x, other_player.y, other_player_new_score - other_player.score, other_player_name))
          other_player.score = other_player_new_score
          other_player.invincibility_end_time = time.time() + 3

      if event_score != 0:
        # the last point in capture_polygon is the intersection point
        self.events.append(Event(capture_polygon[-1].x, capture_polygon[-1].y, event_score, player_name))
        player.score += event_score

  def get_state_json(self):
    events, self.events = self.events, []
    return {
      'server_time': time.time(),
      'players': {name: player.get_state_json() for name, player in self.name_to_player.items()},
      'foods': {id: food.get_state_json() for id, food in self.id_to_food.items()},
      'events': [e.get_state_json() for e in events],
      'tail_lifespan': TAIL_LIFESPAN,
    }

  def schedule_update(self):
    self.loop.create_task(self.update())

  async def update(self):
    for player in self.name_to_player.values():
      player.update();
    for food in self.id_to_food.values():
      food.update();
    while len(self.id_to_food) < MIN_FOODS:
      self.create_food()

    # if a player touches a food, they lose a point and the food is destroyed
    for name, player in self.name_to_player.items():
      if player.invincibility_end_time is not None:
        continue
      destroyed_food_ids = []
      for food_id, food in self.id_to_food.items():
        r = player.r + food.r;
        if dist2(player.x, player.y, food.x, food.y) >= (r * r):
          continue
        if player.score > 0:
          self.events.append(Event(food.x, food.y, -1, name))
          player.score -= 1
        player.invincibility_end_time = time.time() + 3
        destroyed_food_ids.append(food_id)
      for food_id in destroyed_food_ids:
        del self.id_to_food[food_id]

    # broadcast update message to all clients
    state_text = json.dumps({'command': 'update_table_state', 'state': self.get_state_json()})
    tasks = []
    for player in self.name_to_player.values():
      tasks.append(player.ws.send_str(state_text))
    for watcher_ws in self.watchers:
      tasks.append(watcher_ws.send_str(state_text))
    await asyncio.gather(*tasks, return_exceptions=True)
    self.loop.call_later(SECONDS_PER_UPDATE, self.schedule_update)


game = None


async def websocket_handler(request):
  ws = aiohttp.web.WebSocketResponse()
  await ws.prepare(request)

  try:
    player_name = None
    registered = False
    async for ws_message in ws:
      if ws_message.type == aiohttp.WSMsgType.TEXT:
        message = ws_message.json()

        if message['command'] == 'register_player' and not registered:
          player_name = message['name']
          if game.player_exists(player_name):
            await ws.send_json({
              'command': 'error',
              'message': 'Another player with that name is already online.',
              'recoverable': True,
            });
            player_name = None
          else:
            x, y = game.find_spawn_location()
            game.add_player(player_name, x, y, ws)
            registered = True
            # don't send a message; the state update will bring them online

        elif message['command'] == 'register_watcher' and not registered:
          game.add_watcher(ws)
          registered = True
          # don't send a message; the state update will bring them online

        elif message['command'] == 'player_move' and player_name is not None:
          game.on_player_move(player_name, message['x'], message['y'])

        else:
          await ws.send_json({
            'command': 'error',
            'message': 'unrecognized command',
          })

      elif msg.type == aiohttp.WSMsgType.ERROR:
        print('websocket connection closed with exception %s' %
            ws.exception())

      else:
        print('websocket connection sent non-text non-error message')
        break

  finally:
    await ws.close()
    if registered:
      if player_name is not None:
        game.remove_player(player_name)
      else:
        game.remove_watcher(ws)

  return ws


async def index_handler(request):
  raise aiohttp.web.HTTPFound('/static/index.html')


def main(argv):
  global game
  loop = asyncio.get_event_loop()
  game = GameState(loop)

  if len(argv) >= 4:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
    ssl_ctx.load_cert_chain(argv[1], argv[2])
    ssl_ctx.load_verify_locations(cafile=argv[3])
  else:
    ssl_ctx = None

  app = aiohttp.web.Application()
  app.add_routes([
      aiohttp.web.get('/', index_handler),
      aiohttp.web.get('/stream', websocket_handler),
      aiohttp.web.static('/static', './static')])
  aiohttp.web.run_app(app, port=5050, ssl_context=ssl_ctx)

if __name__ == '__main__':
  sys.exit(main(sys.argv))
