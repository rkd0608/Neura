# neura-learn

Neura's evaluate and improve layers (design §28.3, §28.4), run at the site by the site agent in
the worker sandbox:

- `neura_learn.stats`: the paired non-inferiority comparison a release decision rests on;
- `neura_learn.evaluate`: a candidate against the running model, in simulation on the same
  scenes, and on episodes the site's robots recorded (replay);
- `neura_learn.train`: fine-tuning with LeRobot's training loop on a dataset exported from the
  site's episodes;
- `neura-learn evaluate | train`: the job entry point the agent launches.

Models and training need the models group: `uv sync --group models`.
