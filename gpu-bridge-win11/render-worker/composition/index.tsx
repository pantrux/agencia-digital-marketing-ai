import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {Reel} from './Reel.tsx';
import {outputDimensions} from '../src/contracts.ts';

const Root: React.FC = () => <Composition id="AgencyVertical" component={Reel} width={1080} height={1920} fps={30} durationInFrames={90} defaultProps={{title: 'Prueba sintética', subtitle: '', duration_seconds: 3, background: '#102032', accent: '#8fe3c5', watermark: true, video_file: null, resolution: '1080p', transparent: false}} calculateMetadata={({props}) => ({...outputDimensions(props.resolution), durationInFrames: Math.max(1, Math.round(props.duration_seconds * 30))})} />;
registerRoot(Root);
