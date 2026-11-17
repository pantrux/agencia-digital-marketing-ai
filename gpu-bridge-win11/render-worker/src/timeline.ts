import {AD_ENDCARD_FRAME, AD_FRAMES, LABEL_ENTRANCE_FRAMES, SOCIAL_ENDCARD_FRAME, SOCIAL_ENTRANCE_FRAMES, SOCIAL_FADE_FRAME, type EditScene, type SceneLabel, type ScriptedAd} from './contracts.ts';

export function sceneLength(scene: EditScene): number {
  return scene.source_end_frame - scene.source_start_frame;
}

export function graphicFrames(scenes: readonly EditScene[], ad?: ScriptedAd): number[] {
  const changes = new Set([0]);
  let origin = 0;
  const addRange = (start: number, end: number) => {
    for (let frame = start; frame < end; frame += 1) changes.add(frame);
  };
  for (const scene of scenes) {
    if (scene.label) {
      changes.add(origin + scene.label.start_frame);
      changes.add(origin + scene.label.end_frame);
      if (ad?.destination) addRange(origin + scene.label.start_frame,
        origin + Math.min(scene.label.end_frame, scene.label.start_frame + LABEL_ENTRANCE_FRAMES + 1));
    }
    origin += sceneLength(scene);
  }
  if (ad) {
    changes.add(ad.destination ? SOCIAL_ENDCARD_FRAME : AD_ENDCARD_FRAME);
    if (ad.destination) {
      addRange(SOCIAL_ENDCARD_FRAME, SOCIAL_ENDCARD_FRAME + SOCIAL_ENTRANCE_FRAMES + 1);
      addRange(SOCIAL_FADE_FRAME, AD_FRAMES);
    }
    for (const cue of ad.captions) {
      changes.add(cue.start_frame);
      changes.add(cue.end_frame);
    }
  }
  return [...changes].filter(frame => frame < origin).sort((left, right) => left - right);
}

export function graphicLabelTiming(scenes: readonly EditScene[], frame: number): {label: SceneLabel; age: number} | undefined {
  let origin = 0;
  for (const scene of scenes) {
    if (frame < origin + sceneLength(scene)) {
      const label = scene.label, local = frame - origin;
      return label && local >= label.start_frame && local < label.end_frame ? {label, age: local - label.start_frame} : undefined;
    }
    origin += sceneLength(scene);
  }
  return undefined;
}

export function graphicLabel(scenes: readonly EditScene[], frame: number): SceneLabel | undefined {
  return graphicLabelTiming(scenes, frame)?.label;
}
