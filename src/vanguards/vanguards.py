#!/usr/bin/env python

import random
import os
import time
import pickle

import stem

from .NodeSelection import BwWeightedGenerator, NodeRestrictionList
from .NodeSelection import FlagsRestriction
from .rendguard import RendGuard
from .logger import plog

from . import config
from . import control

try:
  xrange
except NameError:
  xrange = range

SEC_PER_HOUR = (60*60)

class GuardNode:
  def __init__(self, idhex, chosen_at, expires_at):
    self.idhex = idhex
    self.chosen_at = chosen_at
    self.expires_at = expires_at

  def __str__(self):
    return self.idhex

  def __repr__(self):
    return self.idhex

class VanguardState:
  def __init__(self):
    self.layer2 = []
    self.layer3 = []
    self.rendguard = RendGuard()

  def sort_and_index_routers(self, routers):
    sorted_r = list(routers)
    dict_r = {}

    for r in sorted_r:
      if r.measured == None:
        # FIXME: Hrmm...
        r.measured = r.bandwidth
    sorted_r.sort(key = lambda x: x.measured, reverse = True)
    for i in xrange(len(sorted_r)): sorted_r[i].list_rank = i
    for r in sorted_r: dict_r[r.fingerprint] = r
    return (sorted_r, dict_r)

  def consensus_update(self, routers, weights):
    (sorted_r, dict_r) = self.sort_and_index_routers(routers)
    ng = BwWeightedGenerator(sorted_r,
                       NodeRestrictionList([FlagsRestriction(["Fast", "Stable"],
                                                             [])]),
                             weights, BwWeightedGenerator.POSITION_MIDDLE)
    gen = ng.generate()
    self.replace_down_guards(dict_r, gen)

    # FIXME: Need to check this more often
    self.replace_expired(gen)
    self.rendguard.xfer_use_counts(ng)

  def new_consensus_event(self, controller, event):
    routers = controller.get_network_statuses()
    consensus_file = os.path.join(controller.get_conf("DataDirectory"),
                             "cached-microdesc-consensus")
    weights = control.get_consensus_weights(consensus_file)
    self.consensus_update(routers, weights)

    self.configure_tor(controller)
    self.write_to_file(open(config.STATE_FILE, "wb"))

  def configure_tor(self, controller):
    if config.NUM_LAYER1_GUARDS:
      controller.set_conf("NumEntryGuards", str(config.NUM_LAYER1_GUARDS))
      try:
        controller.set_conf("NumPrimaryGuards", str(config.NUM_LAYER1_GUARDS))
      except stem.InvalidArguments: # pre-0.3.4 tor
        pass

    if config.LAYER1_LIFETIME:
      controller.set_conf("GuardLifetime", str(config.LAYER1_LIFETIME)+" days")

    controller.set_conf("HSLayer2Nodes", self.layer2_guardset())

    if config.NUM_LAYER3_GUARDS:
      controller.set_conf("HSLayer3Nodes", self.layer3_guardset())

    controller.save_conf()

  def write_to_file(self, outfile):
    return pickle.dump(self, outfile)

  @staticmethod
  def read_from_file(infile):
    return pickle.load(infile)

  def layer2_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer2))

  def layer3_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer3))

  # Adds a new layer2 guard
  def add_new_layer2(self, generator):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer2):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(config.MIN_LAYER2_LIFETIME*SEC_PER_HOUR,
                                       config.MAX_LAYER2_LIFETIME*SEC_PER_HOUR),
                        random.uniform(config.MIN_LAYER2_LIFETIME*SEC_PER_HOUR,
                                       config.MAX_LAYER2_LIFETIME*SEC_PER_HOUR))
    self.layer2.append(GuardNode(guard.fingerprint, now, expires))

  def add_new_layer3(self, generator):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer3):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(config.MIN_LAYER3_LIFETIME*SEC_PER_HOUR,
                                       config.MAX_LAYER3_LIFETIME*SEC_PER_HOUR),
                        random.uniform(config.MIN_LAYER3_LIFETIME*SEC_PER_HOUR,
                                       config.MAX_LAYER3_LIFETIME*SEC_PER_HOUR))
    self.layer3.append(GuardNode(guard.fingerprint, now, expires))

  def _remove_expired(self, remove_from, now):
    for g in list(remove_from):
      if g.expires_at < now:
        remove_from.remove(g)

  def replace_expired(self, generator):
    plog("INFO", "Replacing any old vanguards. Current "+
                 " layer2 guards: "+self.layer2_guardset()+
                 " Current layer3 guards: "+self.layer3_guardset())

    now = time.time()

    self._remove_expired(self.layer2, now)
    self.layer2 = self.layer2[:config.NUM_LAYER2_GUARDS]
    self._remove_expired(self.layer3, now)
    self.layer3 = self.layer3[:config.NUM_LAYER2_GUARDS]

    while len(self.layer2) < config.NUM_LAYER2_GUARDS:
      self.add_new_layer2(generator)

    while len(self.layer3) < config.NUM_LAYER3_GUARDS:
      self.add_new_layer3(generator)

    plog("INFO", "New layer2 guards: "+self.layer2_guardset()+
                 " New layer3 guards: "+self.layer3_guardset())

  def _remove_down(self, remove_from, dict_r):
    removed = []
    for g in list(remove_from):
      if not g.idhex in dict_r:
        remove_from.remove(g)
        removed.append(g)
    return removed

  def replace_down_guards(self, dict_r, generator):
    # If any guards are down, remove them from current
    self._remove_down(self.layer2, dict_r)
    self._remove_down(self.layer3, dict_r)

    while len(self.layer2) < config.NUM_LAYER2_GUARDS:
      self.add_new_layer2(generator)

    while len(self.layer3) < config.NUM_LAYER3_GUARDS:
      self.add_new_layer3(generator)
