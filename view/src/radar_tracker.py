#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雷达目标跟踪与平滑：把逐帧跳变的点云聚类结果变成稳定的目标。

解决的问题：
  1. 点云/聚类中心帧间乱跳 —— 用跨帧匹配 + EMA 位置平滑稳住；
  2. 偶发假目标一闪而过 —— 目标需连续出现若干帧才"确认"输出；
  3. 目标短暂丢失立刻消失 —— 滞后删除，连续丢 N 帧才移除。

纯 Python 实现，不依赖 numpy，树莓派系统 python3 直接可用。

用法::

    from radar_tracker import TargetTracker

    tracker = TargetTracker()
    humans, obstacles = tracker.update(humans, obstacles)
"""

import math
import time


class _Target:
    """一个被跟踪的目标（人体或障碍）。"""

    __slots__ = ('tid', 'kind', 'state', 'x', 'y', 'z', 'v', 'n_points',
                 'confirmed', 'frames_seen', 'frames_missed', 'last_seen')

    def __init__(self, tid, kind, state, x, y, z, v, n_points):
        self.tid = tid
        self.kind = kind          # 'human' | 'obstacle'
        self.state = state        # 'moving' | 'breathing' | 'static'
        self.x = x                # 左右 m
        self.y = y                # 高度 m
        self.z = z                # 前方距离 m
        self.v = v
        self.n_points = n_points
        self.confirmed = False
        self.frames_seen = 0
        self.frames_missed = 0
        self.last_seen = time.time()


class TargetTracker:
    """跨帧跟踪 + 平滑目标位置。

    参数：
      match_dist      两个目标视为"同一个"的最大平面距离 m（默认 0.8）
      pos_alpha      位置 EMA 平滑系数（0~1），越小越平滑、越大越跟手（默认 0.4）
      confirm_frames  新目标连续出现多少帧才确认输出（默认 2，滤假目标）
      lost_frames     目标连续丢失多少帧才删除（默认 5，滞后删除）
      state_smooth    状态切换也做滞回：moving/breathing 需连续 state_frames 帧
                      一致才切换（默认 3）
    """

    def __init__(self, match_dist=0.8, pos_alpha=0.4, confirm_frames=2,
                 lost_frames=5, state_frames=3):
        self.match_dist = match_dist
        self.pos_alpha = pos_alpha
        self.confirm_frames = confirm_frames
        self.lost_frames = lost_frames
        self.state_frames = state_frames
        self._targets = []
        self._next_tid = 1
        self._state_votes = {}   # tid -> {state: count}

    def _match(self, det):
        """找与 det 最近且距离 < match_dist 的已有目标，返回 (idx, dist) 或 None。"""
        best_i = -1
        best_d = self.match_dist
        for i, t in enumerate(self._targets):
            d = math.hypot(det['x'] - t.x, det['z'] - t.z)
            if d < best_d:
                best_d = d
                best_i = i
        return (best_i, best_d) if best_i >= 0 else None

    def _smooth_state(self, t, det_state):
        """状态滞回：连续 state_frames 帧一致才真正切换。"""
        votes = self._state_votes.setdefault(t.tid, {})
        votes[det_state] = votes.get(det_state, 0) + 1
        # 只保留当前票数最多的状态，避免字典无限增长
        if len(votes) > 2:
            top = max(votes, key=votes.get)
            votes = {top: votes[top]}
            self._state_votes[t.tid] = votes
        top = max(votes, key=votes.get)
        if votes[top] >= self.state_frames:
            if t.state != top:
                t.state = top
                votes.clear()
        return t.state

    def update(self, humans, obstacles):
        """输入本帧检测到的人体/障碍，返回平滑后的人体/障碍列表。

        humans    : [{'x','y','z','v','state'}, ...]  显示系坐标
        obstacles : [{'x','y','z','n_points'}, ...]
        """
        # 组装本帧检测（统一成 _Target 输入），标记已匹配
        dets = []
        for h in humans or []:
            dets.append({'kind': 'human', 'state': h['state'],
                         'x': h['x'], 'y': h['y'], 'z': h['z'],
                         'v': h.get('v', 0.0), 'n_points': 1})
        for o in obstacles or []:
            dets.append({'kind': 'obstacle', 'state': 'static',
                         'x': o['x'], 'y': o['y'], 'z': o['z'],
                         'v': 0.0, 'n_points': o.get('n_points', 1)})

        matched = set()

        # 1. 匹配 + 平滑已有目标
        for d in dets:
            hit = self._match(d)
            if hit is None:
                continue
            i, dist = hit
            t = self._targets[i]
            # EMA 平滑位置（用 1-alpha 依赖距离，但简单固定 alpha 即可）
            a = self.pos_alpha
            t.x += a * (d['x'] - t.x)
            t.y += a * (d['y'] - t.y)
            t.z += a * (d['z'] - t.z)
            t.v = d['v']
            t.n_points = d['n_points']
            t.frames_seen += 1
            t.frames_missed = 0
            t.last_seen = time.time()
            if t.frames_seen >= self.confirm_frames:
                t.confirmed = True
            # 人体状态滞回
            if t.kind == 'human':
                self._smooth_state(t, d['state'])
            matched.add(i)

        # 2. 未匹配的新检测 → 新目标（待确认）
        for d in dets:
            hit = self._match(d)
            if hit is None:
                t = _Target(self._next_tid, d['kind'], d['state'],
                            d['x'], d['y'], d['z'], d['v'], d['n_points'])
                self._next_tid += 1
                self._targets.append(t)
                matched.add(len(self._targets) - 1)

        # 3. 未匹配的旧目标 → 丢失计数
        for i, t in enumerate(self._targets):
            if i not in matched:
                t.frames_missed += 1

        # 4. 删除连续丢失超过阈值的目标
        self._targets = [t for t in self._targets
                         if t.frames_missed <= self.lost_frames]

        # 5. 输出已确认目标（未确认的不输出，避免闪烁假目标）
        out_humans = []
        out_obstacles = []
        for t in self._targets:
            if not t.confirmed:
                continue
            d = {'x': round(t.x, 2), 'y': round(t.y, 2), 'z': round(t.z, 2),
                 'v': round(t.v, 2)}
            if t.kind == 'human':
                d['state'] = t.state
                out_humans.append(d)
            else:
                d['n_points'] = t.n_points
                out_obstacles.append(d)
        return out_humans, out_obstacles


class PointAccumulator:
    """点云时间累积：保留最近若干帧的点，让静态障碍物变清晰、滤掉瞬时噪声。

    参数：
      max_frames   保留最近多少帧（默认 4）
      max_age_s    点最长保留时间秒（默认 0.8，防止雷达停发后画面残留）
    """

    def __init__(self, max_frames=4, max_age_s=0.8):
        self.max_frames = max_frames
        self.max_age_s = max_age_s
        self._buf = []   # [(timestamp, [points...]), ...]

    def update(self, points):
        """喂入一帧点，返回累积后的点列表。"""
        now = time.time()
        self._buf.append((now, points))
        # 丢掉过旧的帧（按时间 & 按帧数双重限制）
        while self._buf and now - self._buf[0][0] > self.max_age_s:
            self._buf.pop(0)
        while len(self._buf) > self.max_frames:
            self._buf.pop(0)
        out = []
        for _, pts in self._buf:
            out.extend(pts)
        return out
