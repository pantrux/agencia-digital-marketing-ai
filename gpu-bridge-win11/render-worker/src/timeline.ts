import {adTiming, LABEL_ENTRANCE_FRAMES, type EditScene, type SceneLabel, type ScriptedAd} from './contracts.ts';

export function sceneLength(scene: EditScene): number {
  return scene.source_end_frame - scene.source_start_frame;
}

export function socialFadeProgress(frame: number, duration: number): number {
  const {fadeStart} = adTiming(duration);
  return Math.max(0, Math.min(1, (frame - fadeStart) / Math.max(1, duration - 1 - fadeStart)));
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
    const {voiceEnd, socialStart, entranceFrames, fadeStart} = adTiming(origin);
    changes.add(ad.destination ? socialStart : voiceEnd);
    if (ad.destination) {
      addRange(socialStart, socialStart + entranceFrames + 1);
      addRange(fadeStart, origin);
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
