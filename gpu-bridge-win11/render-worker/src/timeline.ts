import {AD_ENDCARD_FRAME, type EditScene, type SceneLabel, type ScriptedAd} from './contracts.ts';

export function sceneLength(scene: EditScene): number {
  return scene.source_end_frame - scene.source_start_frame;
}

export function graphicFrames(scenes: readonly EditScene[], ad?: ScriptedAd): number[] {
  const changes = new Set([0]);
  let origin = 0;
  for (const scene of scenes) {
    if (scene.label) {
      changes.add(origin + scene.label.start_frame);
      changes.add(origin + scene.label.end_frame);
    }
    origin += sceneLength(scene);
  }
  if (ad) {
    changes.add(AD_ENDCARD_FRAME);
    for (const cue of ad.captions) {
      changes.add(cue.start_frame);
      changes.add(cue.end_frame);
    }
  }
  return [...changes].filter(frame => frame < origin).sort((left, right) => left - right);
}

export function graphicLabel(scenes: readonly EditScene[], frame: number): SceneLabel | undefined {
  let origin = 0;
  for (const scene of scenes) {
    if (frame < origin + sceneLength(scene)) {
      const label = scene.label, local = frame - origin;
      return label && local >= label.start_frame && local < label.end_frame ? label : undefined;
    }
    origin += sceneLength(scene);
  }
  return undefined;
}
