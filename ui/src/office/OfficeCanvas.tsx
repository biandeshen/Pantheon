"use client";

import { Canvas } from "@react-three/fiber";
import { Suspense, useEffect, useRef } from "react";
import * as THREE from "three";
import type { Agent, Task, WhiteboardInfo } from "@/lib/types";
import type { OrgState } from "@/store/reduce";
import type { OfficeDesign } from "./design";
import { OfficeScene } from "./Scene";

/**
 * Make sure the WebGL canvas gets a real size on first paint.
 *
 * The defect, measured on a real windowed Chrome on 2026-11-17
 * (1280x900, dpr 2, org page):
 *
 *   app div        787 x 377   (relative, has height)
 *   R3F outer      787 x 377   (relative, width/height 100%)
 *   R3F container  787 x 377   (width/height 100%)
 *   <canvas>       300 x 150   <- HTMLCanvasElement default
 *
 * Every ancestor had a resolved box; only the canvas kept its default. Its
 * inline style was `display: block` with no width/height, because
 * react-three-fiber derives the canvas size from react-use-measure, and that
 * first measurement had not landed. Dispatching one window resize fixed it
 * immediately: 787x377 CSS, 1377x659 drawing buffer.
 *
 * So this hook does that, deterministically. After mount it watches the wrapper
 * with a ResizeObserver and, while the canvas is still smaller than its
 * container, dispatches a resize so R3F re-measures. It stops as soon as the
 * sizes agree, so it never fights a genuine user resize.
 *
 * Why not set width/height on the canvas element directly? R3F owns that
 * attribute -- it rewrites it from the measured size, so an externally forced
 * value is overwritten and the camera aspect would disagree with the buffer.
 */
function useCanvasSizeRecovery(containerRef: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let done = false;
    let attempts = 0;
    const MAX_ATTEMPTS = 12;

    const tooSmall = () => {
      const canvas = container.querySelector("canvas");
      if (!canvas) return true;
      const box = canvas.getBoundingClientRect();
      const parent = container.getBoundingClientRect();
      if (parent.width <= 4 || parent.height <= 4) return true;
      return Math.abs(box.width - parent.width) > 1 || Math.abs(box.height - parent.height) > 1;
    };

    const nudge = () => {
      if (done) return;
      if (!tooSmall()) {
        done = true;
        observer.disconnect();
        return;
      }
      attempts += 1;
      window.dispatchEvent(new Event("resize"));
      if (attempts >= MAX_ATTEMPTS) {
        // Give up quietly: a layout that never resolves a size must not spin.
        done = true;
        observer.disconnect();
      }
    };

    const observer = new ResizeObserver(() => nudge());
    observer.observe(container);

    // One pass on the next frame, one after fonts/layout settle.
    const raf = requestAnimationFrame(nudge);
    const timer = window.setTimeout(nudge, 300);

    return () => {
      done = true;
      observer.disconnect();
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, [containerRef]);
}

export default function OfficeCanvas(props: {
  design: OfficeDesign;
  editing: boolean;
  agents: Agent[];
  state: Pick<OrgState, "live" | "meetings" | "beams" | "agents" | "objects">;
  tasks: Task[];
  selectedId: string | null;
  focusId: string | null;
  onSelect: (id: string | null) => void;
  onBoardClick: () => void;
  whiteboards?: WhiteboardInfo[];
  onWhiteboardClick?: (boardId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  useCanvasSizeRecovery(containerRef);

  return (
    <div ref={containerRef} style={{ width: "100%", height: "100%" }}>
      <Canvas
        shadows={{ type: THREE.PCFShadowMap }}
        dpr={[1, 1.75]}
        camera={{ fov: 40, near: 0.1, far: 400, position: [20, 25, 40] }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        style={{ width: "100%", height: "100%", display: "block" }}
        onPointerMissed={() => !props.editing && props.onSelect(null)}
      >
        <Suspense fallback={null}>
          <OfficeScene {...props} />
        </Suspense>
      </Canvas>
    </div>
  );
}
