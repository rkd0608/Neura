"""The same policy driving the same arm with and without neura-robot, under the same conditions
(a steady GPU server, latency spikes, a network drop, a policy crash, an operator takeover, and a
real model's measured answer times), and what each loop sent the arm: ``python -m
neura_robots.compare`` writes the report. ``loops`` holds the two loops, ``scenarios`` the
conditions, ``metrics`` what is measured, ``report`` the write-up.
"""
