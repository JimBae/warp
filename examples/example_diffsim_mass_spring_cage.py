# Copyright (c) 2023 NVIDIA CORPORATION.  All rights reserved.
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

###########################################################################
# Example Diff Sim Spring Cage
#
# A single particle is attached with springs to each point of a cage.
# The objective is to optimize the rest length of the springs in order
# for the particle to be pulled towards a target position.
#
###########################################################################

import os

import numpy as np
import warp as wp


wp.init()


@wp.kernel
def compute_loss_kernel(
    pos: wp.array(dtype=wp.vec3),
    target_pos: wp.vec3,
    loss: wp.array(dtype=float),
):
    loss[0] = wp.length_sq(pos[0] - target_pos)


@wp.kernel(enable_backward=False)
def apply_gradient_kernel(
    spring_rest_lengths_grad: wp.array(dtype=float),
    train_rate: float,
    spring_rest_lengths: wp.array(dtype=float),
):
    tid = wp.tid()

    spring_rest_lengths[tid] -= spring_rest_lengths_grad[tid] * train_rate


class Example:
    def __init__(
        self,
        stage=None,
        enable_rendering=True,
        verbose=False,
    ):
        import warp.sim

        # Duration of the simulation, in seconds.
        duration = 1.0

        # Number of frames per second.
        self.fps = 30.0

        # Duration of a single simulation iteration in number of frames.
        self.frame_count = int(duration * self.fps)

        # Number of simulation steps to take per frame.
        self.sim_substep_count = 1

        # Delta time between each simulation substep.
        self.sim_dt = 1.0 / (self.fps * self.sim_substep_count)

        # Target position that we want the main particle to reach by optimising
        # the rest lengths of the springs.
        self.target_pos = (0.125, 0.25, 0.375)

        # Number of training iterations.
        self.train_iters = 100

        # Factor by which the rest lengths of the springs are adjusted after each
        # iteration, relatively to the corresponding gradients. Lower values
        # converge more slowly but have less chances to miss the local minimum.
        self.train_rate = 0.5

        # Initialize the helper to build a physics scene.
        builder = warp.sim.ModelBuilder()

        # Define the main particle at the origin.
        particle_mass = 1.0
        builder.add_particle((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), particle_mass)

        # Define the cage made of points that will be pulling our main particle
        # using springs.
        # fmt: off
        builder.add_particle((-0.7,  0.8,  0.2), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle(( 0.0,  0.2,  1.1), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle(( 0.1,  0.1, -1.2), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle(( 0.6,  0.4,  0.4), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle(( 0.7, -0.9, -0.2), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle((-0.8, -0.8,  0.1), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle((-0.9,  0.2, -0.8), (0.0, 0.0, 0.0), 0.0)
        builder.add_particle(( 1.0,  0.4, -0.1), (0.0, 0.0, 0.0), 0.0)
        # fmt: on

        # Define the spring constraints between the main particle and the cage points.
        spring_elastic_stiffness = 100.0
        spring_elastic_damping = 10.0
        for i in range(1, builder.particle_count):
            builder.add_spring(0, i, spring_elastic_stiffness, spring_elastic_damping, 0)

        # Build the model and set-up its properties.
        self.model = builder.finalize(requires_grad=True)
        self.model.gravity = np.array((0.0, 0.0, 0.0))
        self.model.ground = False

        # Use the Euler integrator for stepping through the simulation.
        self.integrator = warp.sim.SemiImplicitIntegrator()

        # Initialize a state for each simulation step.
        self.states = tuple(
            self.model.state(requires_grad=True) for _ in range(self.frame_count * self.sim_substep_count + 1)
        )

        # Initialize a loss value that will represent the distance of the main
        # particle to the target position. It needs to be defined as an array
        # so that it can be written out by a kernel.
        self.loss = wp.zeros(1, dtype=float, requires_grad=True)

        if enable_rendering:
            import warp.sim.render

            # Helper to render the physics scene as a USD file.
            self.renderer = warp.sim.render.SimRenderer(self.model, stage, fps=self.fps)

            # Allows rendering one simulation to USD every N training iterations.
            self.render_iteration_steps = 2

            # Frame number used to render the simulation iterations onto the USD file.
            self.render_frame = 0
        else:
            self.renderer = None

        use_cuda_graph = True
        if use_cuda_graph:
            # Capture all the kernel launches into a CUDA graph so that they can
            # all be run in a single graph launch, which helps with performance.
            wp.capture_begin()
            try:
                self.simulate()
            finally:
                self.graph = wp.capture_end()
        else:
            self.graph = None

        self.verbose = verbose

    def compute_loss(self) -> None:
        for i in range(1, len(self.states)):
            prev = self.states[i - 1]
            curr = self.states[i]
            prev.clear_forces()
            self.integrator.simulate(
                self.model,
                prev,
                curr,
                self.sim_dt,
            )

        last_state = self.states[-1]
        wp.launch(
            compute_loss_kernel,
            dim=1,
            inputs=(
                last_state.particle_q,
                self.target_pos,
            ),
            outputs=(self.loss,),
        )

    def simulate(self):
        tape = wp.Tape()
        with tape:
            self.compute_loss()

        tape.backward(loss=self.loss)

        wp.launch(
            apply_gradient_kernel,
            dim=self.model.spring_count,
            inputs=(
                self.model.spring_rest_length.grad,
                self.train_rate,
            ),
            outputs=(self.model.spring_rest_length,),
        )

        tape.zero()

    def update(self):
        if self.graph is None:
            self.simulate()
        else:
            wp.capture_launch(self.graph)

    def render(self):
        if self.renderer is None:
            return

        self.renderer.begin_frame(0.0)
        self.renderer.render_box(
            name="target",
            pos=self.target_pos,
            rot=wp.quat_identity(),
            extents=(0.1, 0.1, 0.1),
        )
        self.renderer.end_frame()

        for frame in range(self.frame_count):
            self.renderer.begin_frame(self.render_frame / self.fps)
            self.renderer.render(self.states[frame * self.sim_substep_count])
            self.renderer.end_frame()

            self.render_frame += 1

    def run(self) -> None:
        for iteration in range(self.train_iters):
            self.update()

            loss = self.loss.numpy()[0]

            if self.verbose:
                print(f"[{iteration:3d}] loss={loss:.8f}")

            if (
                iteration == self.train_iters - 1
                or iteration % self.render_iteration_steps == 0
            ):
                self.render()

        if self.renderer is not None:
            self.renderer.save()


if __name__ == "__main__":
    this_dir = os.path.realpath(os.path.dirname(__file__))
    this_file = os.path.basename(__file__).split(".")[0]
    stage_path = os.path.join(this_dir, "outputs", f"{this_file}.usda")

    with wp.ScopedDevice("cuda:0"):
        example = Example(stage_path, verbose=True)
        example.run()
