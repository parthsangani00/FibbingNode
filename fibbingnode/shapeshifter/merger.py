import utils as ssu
import fibbingnode
import sys
import abc
import collections
import itertools
import logging


log = fibbingnode.log
log.setLevel(logging.DEBUG)


class Node(object):
    GLOBAL = 'global'  # Globally visible fake node
    LOCAL = 'local'   # Locally-scoped fake node

    def __init__(self, lb=0, ub=0, fake=None, name=None,
                 original_nhs=None, forced_nhs=None):
        self.lb = lb  # Lower bound
        self.ub = ub  # Upper bound
        self.fake = fake  # Fake node type if any
        self.forced_nhs = forced_nhs if forced_nhs else set()
        self.original_nhs = original_nhs
        self.name = name

    def add_fake_node(self, type=GLOBAL):
        """Attach a fake node to this node
        :param type: Whether the node should be locally or globally visible"""
        self.fake = type

    def has_fake_node(self, subtype=None):
        """Whether this node has a fake or not
        :param subtype: check for a particular type of fake node"""
        return bool(self.forced_nhs) and (self.fake == subtype if
                                          subtype else True)

    def __repr__(self):
        if self.forced_nhs:
            return '%s - Fake: %s ]%s,%s[ %s' % (self.name, self.fake, self.lb,
                                                 self.ub, self.forced_nhs)
        else:
            return '%s - Fixed: %s' % (self.name, self.original_nhs)

    @staticmethod
    def increase_lb(n, lb):
        n.lb += lb

    @staticmethod
    def setlocal(n):
        log.debug('Converting the fake node of %s to a '
                  'locally scoped one', n)
        n.fake = Node.LOCAL


class Merger(object):
    def __init__(self):
        self.new_edge_weight = 10e3  # Default cost for new edges in the graph
        self.g = self._p = self.dag = self.dest = self.reqs = None
        self.ecmp = collections.defaultdict(set)

    def solve(self, graph, requirements):
        """Compute the augmented topology for a given graph and a set of
        requirements.
        :type graph: DiGraph
        :type requirements: { dest: DiGraph }
        :param requirements: the set of requirement DAG on a per dest. basis
        :return: list of fake LSAs"""
        self.reqs = requirements
        log.info('Preparing IGP graph')
        self.g = prepare_graph(graph, requirements)
        log.info('Computing SPT')
        self._p = ssu.all_shortest_paths(self.g)
        lsa = []
        for dest, dag in requirements.iteritems():
            self.ecmp.clear()
            log.info('Evaluating requirement %s', dest)
            self.dest = dest
            self.dag = dag
            log.info('Ensuring the consistency of the DAG')
            self.check_dest()
            self.complete_dag()
            log.info('Computing original and required next-hop sets')
            for n, node in self.nodes():
                node.forced_nhs = set(self.dag.successors(n))
                node.original_nhs = set([p[1] for p in
                                         self.path(n, self.dest)])
            if not self.check_consistency():
                log.warning('Consistency check failed, skipping %s', dest)
                continue
            log.info('Placing initial fake nodes')
            self.place_fake_nodes()
            log.info('Initializing fake nodes')
            self.initialize_fake_nodes()
            log.info('Propagating initial lower bounds')
            self.propagate_lb()
            log.info('Reducing the augmented topology')
            self.merge_fake_nodes()
            log.info('Generating LSAs')
            lsas = self.create_fake_lsa()
            log.info('Solved the DAG for destination %s with LSA set: %s',
                     self.dest, lsas)
            lsa.extend(lsas)
        return lsa

    #
    # Implementation section
    #

    def path(self, src, dst):
        """Return the list of shortest paths between src and dst"""
        return self._p[src][0][dst]

    def has_path(self, s, d):
        """Return whether there exists a path between s and d"""
        try:
            return bool(self.path(s, d))
        except:
            return False

    def cost(self, src, dst):
        """Return the cost of the shortest paths between src and dst"""
        return self._p[src][1][dst]

    def register_path(self, src, dst, path, cost):
        """Register a shortest path between src and dst, with a given cost.
        path is the set of paths between src and dst"""
        try:
            src_tuple = self._p[src]
            src_tuple[1][dst] = cost
            src_tuple[0][dst] = path
        except KeyError:
            log.debug('Registering a new src %s for paths %s', src, path)
            self._p[src] = ({dst: path}, cost)

    def check_dest(self):
        """Check that the destination is present in the DAG and the graph"""
        dest_in_graph = self.dest in self.g
        log.debug('Checking for %s in the graph: %s', self.dest, dest_in_graph)
        if not dest_in_graph:
            log.info('Adding %s in the graph', self.dest)
            self.g.add_node(self.dest, data=Node())
            new_paths = {}
            new_paths_cost = {n: sys.maxint for n in self.g.nodes_iter()}
        dest_in_dag = self.dest in self.dag
        log.debug('Checking for the presence of %s the the DAG: %s',
                  self.dest, dest_in_dag)
        if not dest_in_dag or not dest_in_graph:
            if not dest_in_dag:
                sinks = ssu.find_sink(self.dag)
            else:
                sinks = self.dag.predecessors(self.dest)
            for s in sinks:
                if not dest_in_dag:
                    log.info('Adding %s to %s in the dag', self.dest, s)
                    self.dag.add_edge(s, self.dest)
                if not dest_in_graph:
                    log.info('Adding edge (%s, %s) in the graph',
                             s, self.dest)
                    self.g.add_edge(s, self.dest, weight=self.new_edge_weight)
                    log.debug('Updating spt/cost accordingly')
                    for n in self.g.nodes_iter():
                        if n == self.dest:
                            new_paths[n] = [[n]]
                            new_paths_cost[n] = 0
                            continue
                        if not self.has_path(n, s):
                            continue
                        ns_cost = self.cost(n, s) + self.new_edge_weight
                        if ns_cost < new_paths_cost[n]:  # Created a new SP
                            ns_path = self.path(n, s)
                            new_paths_cost[n] = ns_cost
                            new_paths[n] = list(ssu.extend_paths_list(ns_path,
                                                                      self.dest
                                                                      ))
                        elif ns_cost == new_paths_cost:  # Created ECMP
                            ns_path = self.path(n, s)
                            new_paths[n].extend(ssu.extend_paths_list(ns_path,
                                                                      self.dest
                                                                      ))
        for n, p in new_paths.iteritems():  # Incrementally update the SPT
            self.register_path(n, self.dest, p, new_paths_cost[n])

    def check_consistency(self):
        """Checks that the DAG can be embedded in the graph"""
        log.debug('Checking consitency between the dag and the graph')
        for u, v in self.dag.edges_iter():
            if not self.g.has_edge(u, v):
                log.error('Edge (%s, %s) not found in the graph',  u, v)
                return False
        return True

    @abc.abstractmethod
    def place_fake_nodes(self):
        """Place the Fake nodes on the graph"""

    def initialize_fake_nodes(self):
        self.initialize_ecmp_deps()
        self.compute_initial_lb()
        self.compute_initial_ub()

    def initialize_ecmp_deps(self):
        """Initialize ECMP dependencies"""
        for n, node in map(lambda x: (x[0], self.node(x[0])),
                           filter(lambda x: x[1] > 1,
                                  self.dag.out_degree_iter())):
            if node.has_fake_node():
                log.debug('%s does ECMP and has a fake node', n)
                self.ecmp[n].add(n)
            else:
                f = []
                paths = self.paths(n, self.dest)
                for p in paths:
                    # Try to find the first fake node for each path
                    for h in p[:-1]:
                        if self.node(h).has_fake_node():
                            f.add(h)
                            break
                if len(f) > 0 and len(f) < len(paths):
                    log.warning('%s does ECMP and has less downstream fake '
                                'nodes than paths (%s < %s), forcing it to '
                                'have a fake node.', n, len(f), len(paths))
                    node.fake_type = Node.GLOBAL
                elif f:
                    log.debug('Registering ECMP depencies on %s: %s', n, f)
                    for fake in f:
                        self.ecmp[fake].add(f)

    def compute_initial_lb(self):
        """Set the initial values for the lb on every node having a fake node
        BFS from the dest until nodes having a fake node"""
        visited = set()
        to_visit = set(self.g.predecessors_iter(self.dest))
        while to_visit:
            node_name = to_visit.pop()
            log.debug('Exploring %s', node_name)
            if node_name in visited:
                continue
            visited.add(node_name)
            n = self.node(node_name)
            if n.has_fake_node():
                lb = self.initial_lb_of(node_name)
                log.debug('Setting initial lb of %s to: %s', node_name, lb)
                n.lb = lb
            else:
                log.debug('%s does not have a fake node, exploring neighbours',
                          node_name)
                to_visit |= set(self.g.predecessors_iter(node_name))

    def initial_lb_of(self, node):
        """Compute the initial lower bound of a node"""
        lb = 0
        for nei in self.g[node]:
            if nei in self.reqs:
                log.debug('Not considering %s for initial LB of %s as '
                          'it is a destination', nei, node)
                continue
            if self.node(nei).has_fake_node():
                log.debug('Not considering %s for initial LB of %s as '
                          'it has a fake node', node, nei)
                continue
            if self.dag.has_edge(nei, node):
                log.debug('Not considering %s for initial LB of %s as '
                          '%s->%s exists in the DAG', nei, node, nei, node)
                continue
            nei_dest_paths = self.path(nei, self.dest)
            if not nei_dest_paths:
                log.debug('Not considering %s for initial LB of %s as '
                          'it has no path to the destination', nei, node)
                continue
            # Track whether nei has a path without fake nodes to the dest
            has_pure_path = False
            # Track whether node is in the spt of nei to dest
            node_in_spt = False
            for p in nei_dest_paths:
                # Status of this path
                is_pure = True
                for n in p[:-1]:
                    if self.node(n).has_fake_node():
                        is_pure = False
                        break
                    if node == n:
                        log.debug('Not considering %s for initial LB of %s as '
                                  '%s is in its shortest path to the '
                                  'destination %s', nei, node, node, p)
                        node_in_spt = True
                        break
                if node_in_spt:  # Already logged the cause
                    break
                has_pure_path = has_pure_path or is_pure
            if node_in_spt:
                continue
            if not has_pure_path:
                log.debug('Not considering %s for initial LB of %s as '
                          'it does not have a path to the destination without '
                          'the presence of fake nodes.', nei, node)
                continue
            nei_lb = self.cost(nei, self.dest) - self.cost(nei, node)
            if nei_lb > lb:
                lb = nei_lb
                log.debug('Initial LB of %s set to %s by %s',
                          node, lb, nei)
        return lb

    def compute_initial_ub(self):
        for n, node in self.nodes(Node.GLOBAL):
            node.ub = self.cost(n, self.dest)
            log.debug('Initial ub of %s set to %s', n, node.ub)

    def propagate_lb(self, assign=Node.increase_lb, fail_func=Node.setlocal,
                     initial_nodes=None):
        """Propagate the lower bounds of nodes accross the graph
        :type assign: function(node, new_lb)
        :param assign: The function to call when a new lb has to be set
        :type fail_func: function(node)
        :param fail_func: The function to call when a node has
                          conflicting bounds. If it returns anything, abort
                          the propagation.
        :type initial_nodes: list
        :param initial_nodes: The initial set of nodes to propagare from,
                              or all the nodes if set to None"""
        pq = ssu.MaxHeap([(self.get_delta(n), n) for n in
                          ([n for n, _ in self.nodes(Node.GLOBAL)]
                           if not initial_nodes else initial_nodes)])
        log.debug('Initial PQ: %s', pq)
        updates = set()
        while not pq.is_empty():
            # Get the node with the biggest influence potential
            delta, node = pq.pop()
            # Check that we did not already check it before
            if delta < self.get_delta(node):
                # This node has already been updated
                log.debug('Ignoring delta %s for %s', delta, node)
                continue
            log.debug('Evaluating %s', node)
            fixed_neighbors = self.fixed_nodes_for(node)
            # Explore its neighbors
            for n, nei in self.fake_neighbors(node):
                # Compute the cost needed by that neighbor to not attract us
                lb_diff = self.inherit_lb(n, node, fixed_neighbors) - nei.lb
                if lb_diff > 0:
                    failed = False
                    if (node, n) in updates:
                        log.debug('The propagation of the LB of %s to %s '
                                  'caused an influence loop, failing it!',
                                  node, n)
                        failed = True
                    else:
                        updates.add((node, n))
                        log.debug('%s causes the LB of %s to increase by %s',
                                  node, n, lb_diff)
                        if nei.lb + lb_diff + 1 < nei.ub:
                            assign(nei, lb_diff)
                            # Schedule the neighbor for update
                            pq.push((self.get_delta(n), n))
                            # Also take care of the ECMP deps.
                            for e in self.ecmp_dep(n):
                                if e == n:
                                    continue
                                e_node = self.node(e)
                                if e_node.lb + lb_diff + 1 < e_node.ub:
                                    log.debug('Increasing the LB of %s', e)
                                    assign(e_node, lb_diff)
                                    # Schedule the neighbor for update
                                    pq.push((self.get_delta(e), e))
                                else:
                                    failed = True
                                    break
                        else:
                            failed = True
                    if failed:
                        if fail_func(self.node(n)):
                            return
                        map(fail_func, map(self.node, self.ecmp_dep(nei)))

    def fixed_nodes_for(self, n):
        """Return the list of all nodes without a fake node that rely on the
        fake node of n"""
        fixed_nodes = set()
        stack = [(p, n) for p in self.dag.predecessors(n)]
        while stack:
            u, v = stack.pop()
            if v in self.node(u).forced_nhs:  # u has a fake node towards v
                continue
            elif u in fixed_nodes:  # we already saw u
                continue
            else:
                fixed_nodes.add(u)
                stack.extend([(p, u) for p in self.dag.predecessors(u)])
        return fixed_nodes

    def get_delta(self, n):
        """Return the delta value associated to that node,
        that is the potential it has to influence another fakenode lb."""
        links_to_fn = [self.cost(n, nei) for nei, _ in self.fake_neighbors(n)]
        return (self.node(n).lb - min(links_to_fn)) if links_to_fn else 0

    def inherit_lb(self, node, from_node, fixed_neighbors):
        """Return the LB to set on node based on the one from from_node"""
        lb_base = self.node(from_node).lb
        lb = max(map(lambda n: (self.cost(from_node, n) - self.cost(n, node) +
                                (1 if not self.dag_include_spt(n, node)
                                 else 0)),
                     itertools.chain([from_node], fixed_neighbors)))
        return lb_base + lb

    def merge_fake_nodes(self):
        """Attempt to reduce the number of fake nodes by merging successive
        ones into each other"""
        dag_spt = ssu.dag_paths_from_leaves(self.dag, self.dest)
        for path in dag_spt:
            log.debug('Trying to merge along %s', path)
            fake_nodes = [n for n in path[:-1]
                          if self.node(n).has_fake_node()]
            for idx, n in enumerate(fake_nodes[:-1]):
                succ = fake_nodes[idx+1]
                if self.node(n).fake == Node.GLOBAL\
                   and self.node(succ).fake == Node.GLOBAL:
                    self.merge(n, succ, path, idx)

    def merge(self, n, succ, path, n_idx):
        """Try to merge n into its successor fake node, along the given path"""
        log.debug('Trying to merge %s into %s', n, succ)
        if not self.dag_include_spt(n, succ):
            return  # at least one IGP SP is not included in the DAG
        try:
            new_lb, new_ub = self.combine_ranges(n, succ)
            log.debug('Merging %s into %s would result in bounds in %s set to '
                      ']%s, %s[', n, succ, succ, new_lb, new_ub)
            self.apply_merge(n, succ, new_lb, new_ub, path[n_idx + 1])
        except TypeError:  # Couldn't find a valid range, skip
            return

    def dag_include_spt(self, n, s):
        """Check if all SP from n to s in the graph are also in the DAG"""
        for p in self.path(n, s):
            for u, v in zip(p[:-1], p[1:]):
                if not self.dag.has_edge(u, v):
                    log.debug('(%s, %s) is in the SP set of %s->%s '
                              'but not in the DAG', u, v, n, s)
                    return False
                if v == s:
                    # We reached the target node, and it is included in the DAG
                    break
                # Check that dag <=> SPT
        return True

    def combine_ranges(self, n, s):
        """Attempt to combine the lb,ub interval between the two nodes"""
        node, succ = self.node(n), self.node(s)
        cost = self.cost(n, s)
        new_ub = min(node.ub - cost, succ.ub)
        new_lb = max(node.lb - cost, succ.lb)
        # Log these errors which should never happen
        # as propagation should prevent this
        if new_lb > succ.lb:
            log.error('Merging %s into %s resulted in a LB increase from '
                      '%s to %s (%s''s LB: %s, spt cost: %s)',
                      n, s, succ.lb, new_lb, node.lb, cost)
        elif new_lb < succ.lb:
            log.error('Merging %s into %s resulted in a LB decrease from '
                      '%s to %s (%s''s LB: %s, spt cost: %s)',
                      n, s, succ.lb, new_lb, node.lb, cost)
        # Report unfeasibible merge
        if not new_lb + 1 < new_ub:
            log.debug('Merging %s into %s would lead to bounds of '
                      ']%s, %s[, aborting', n, s, new_lb, new_ub)
            return None
        return new_lb, new_ub

    def apply_merge(self, n, s, lb, ub, nh):
        """Try to apply a given merge, n->s, with new lb/ub for s,
        and corresponding to the nexthop of n nh"""
        undos = []
        propagation_failure = []

        def undo_all():
            log.debug('Undoing all changes')
            for (f, args) in reversed(undos):
                f(*args)

        def record_undo(f, *args):
            undos.append((f, args))

        def propagation_fail(n):
            log.debug('The propagation failed on node %s, aborting merge!', n)
            propagation_failure.append(False)
            return True

        def propagation_assign(node, lb):
            record_undo(setattr, node, 'lb', node.lb)
            log.debug('Propagation caused the LB of %s to increase by %s',
                      n, lb)
            Node.increase_lb(node, lb)

        log.debug('Trying to apply merge, n: %s, s:%s, lb:%s, ub:%s, nh:%s',
                  n, s, lb, ub, nh)
        # Remove the fake node
        node = self.node(n)
        node.forced_nhs.remove(nh)
        record_undo(node.forced_nhs.add, nh)

        # Update the values in its successor
        succ_node = self.node(s)
        path_cost_increase = self.cost(n, s) + succ_node.lb - node.lb
        record_undo(setattr, succ_node, 'lb', succ_node.lb)
        record_undo(setattr, succ_node, 'ub', succ_node.ub)
        succ_node.lb = lb
        succ_node.ub = ub

        ecmp_deps = list(self.ecmp_dep(n))
        log.debug('Checking merge effect on ECMP dependencies of %s: %s',
                  n, ecmp_deps)
        if s in ecmp_deps:
            log.debug('Aborting merge has %s and %s are ECMP dependent: '
                      'Merging them would make it impossible to keep both path'
                      ' with the same cost!', n, s)
            undo_all()
            return
        remove_n = not node.has_fake_node()
        if remove_n:
            log.debug('Also removing %s from its ECMP deps has it no longer '
                      'has a fake node.', n)
        deps = self.ecmp[s]
        for e in ecmp_deps:
            e_node = self.node(e)
            e_deps = self.ecmp[e]
            if remove_n:
                e_deps.remove(n)
                record_undo(e_deps.add, n)
                if e == n:
                    continue
            if e not in deps:
                deps.add(e)
                record_undo(deps.remove, e)
            if s not in e_deps:
                e_deps.add(s)
                record_undo(e_deps.remove, s)
            new_lb = e_node.lb + path_cost_increase
            if not new_lb + 1 < e_node.ub:
                log.debug('Cannot increase the ECMP ecmp dep %s of %s by %s. '
                          'Aborting merge!', e, n, path_cost_increase)
                undo_all()
                return
            else:
                log.debug('Increased %s to %s', e, new_lb)
                record_undo(setattr, e_node, 'lb', e_node.lb)
                e_node.lb = new_lb

        ecmp_deps.append(s)
        log.debug('Propagating LB changes')
        self.propagate_lb(assign=propagation_assign,
                          fail_func=propagation_fail,
                          initial_nodes=ecmp_deps)
        if propagation_failure:
            undo_all()
        else:
            log.info('Merged %s into %s', n, s)

    def create_fake_lsa(self):
        lsa = []
        for n in self.dag:
            if n == self.dest:
                continue
            node = self.node(n)
            for nh in node.forced_nhs:
                lsa.append(ssu.LSA(node=n,
                                   nh=nh,
                                   cost=node.lb + 1
                                   if node.fake == Node.GLOBAL else -1,
                                   dest=self.dest))
        return lsa

    def nodes(self, fake_type=None):
        """Iterate over the nodes of the graph for the current dest
        :param fake_type: if not None, restrict to nodes having that kind of
                          fake node"""
        for n, data in self.g.nodes_iter(data=True):
            if n in self.reqs:
                continue  # Skip the destination nodes
            node = data['data'][self.dest]
            if not fake_type or node.has_fake_node(fake_type):
                yield n, node

    def node(self, n):
        """Return the Node for a given node name, for the current dest"""
        try:
            return self.g.node[n]['data'][self.dest]
        except KeyError:
            return None

    def fake_neighbors(self, node):
        """Iterator over all fake nodes reachable from node
        :return: iter((name, node))"""
        visited = set()
        to_visit = set(self.g.neighbors(node))
        while to_visit:
            n = to_visit.pop()
            if n in visited:
                continue
            visited.add(n)
            n_node = self.node(n)
            if n_node.has_fake_node(subtype=Node.GLOBAL):
                yield n, n_node
            else:
                to_visit |= set(self.g.neighbors(node))

    def ecmp_dep(self, node):
        """Iterates over the ECMP dependencies of n"""
        return self.ecmp[node]

    def complete_dag(self):
        """Complete the DAG so that missing nodes have their old (or part of)
        SPT in it"""
        for n in self.g:
            if n in self.dag or n in self.reqs:
                continue  # n has its SPT instructions or is a destination node
            for p in self.path(n, self.dest):
                for u, v in zip(p[:-1], p[1:]):
                    v_in_dag = v in self.dag
                    self.dag.add_edge(u, v)
                    if v_in_dag:  # we connected u to the new SPT
                        break


def prepare_graph(g, req):
    """Copy the given graph and preset nodes attribute
    :type g: DiGraph
    :return: DiGraph
    :type req: {dest: fwd_req}
    :param req: The requirements for that graph"""
    log.debug('Copying graph')
    graph = g.copy()
    for n in graph.nodes():
        graph.node[n]['data'] = {key: Node(name=n) for key in req}
    return graph


class FullMerger(Merger):
    """Add a fake node to every node in the graph
        except those with at most one outgoing link"""

    def place_fake_nodes(self):
        map(self.add_fake_node,
            filter(self.fake_node_candidate), self.g.out_degree_iter())

    def fake_node_candidate(self, candidate):
        """Filter out nodes that cannot change their nexthop anyway
        :type candidate: (node, out_degree)"""
        return candidate[1] > 1

    def add_fake_node(self, candidate):
        """Add a fake node on the graph
        :type candidate: (node, out_degree)"""
        self.node(candidate[0]).add_fake_node()
        log.debug('Adding a fake node on %s', candidate[0])


class PartialMerger(Merger):
    """Add a fake node only on the nodes that needs to change their nexthop"""

    def place_fake_nodes(self):
        for n in self.dag.nodes_iter():
            if self.g.out_degree(n) > 1:
                node = self.node(n)
                if self.needs_fake_node(node.original_nhs, node.forced_nhs):
                    node.add_fake_node()
                    log.debug('Adding a fake node on %s', n)
                else:
                    node.forced_nhs = set()
                    log.debug('Skipping %s has it keeps the same successors',
                              n)

    @staticmethod
    def needs_fake_node(orig, dag):
        """Return whether this node needs a fake node, based on the old and new
        successors set.
        :type orig: set
        :type dag: set"""
        return orig.symmetric_difference(dag)


class PartialECMPMerger(PartialMerger):
    """Add a fake node if ECMP is required
    or if the two successor sets are different"""

    @staticmethod
    def needs_fake_node(orig, dag):
        return len(dag) > 1 or orig.symmetric_difference(dag)
