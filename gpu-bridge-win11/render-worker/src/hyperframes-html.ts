import {
  adTiming, WorkerError, type RenderRequest,
} from './contracts.ts';
import {socialIcons} from './social-icons.ts';
import {sceneLength} from './timeline.ts';

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/gu, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]!));
}

function timed(start: number, end: number, track: number): string {
  return `data-start="${start / 30}" data-duration="${(end - start) / 30}" data-track-index="${track}"`;
}

const styles = `
*{box-sizing:border-box}html,body{margin:0;background:transparent}
#main{position:relative;overflow:hidden;color:#fff;font:42px Arial,sans-serif}
#layout{position:absolute;inset:0;width:1080px;height:1920px;transform-origin:0 0}
video{position:absolute;width:100%;height:100%;object-fit:cover}
.label,.captions,.social{position:absolute;max-width:860px;text-shadow:0 2px 12px #10241a}
.label{top:165px;left:70px;font-size:63px;font-weight:800;line-height:1.06;overflow-wrap:anywhere;animation:arrive .32s cubic-bezier(.16,1,.3,1) both}
.label.first{top:120px;left:auto;right:118px;max-width:390px;font-size:52px;text-align:left}
.label.bottom{top:auto;bottom:541px}
.label::after{display:block;content:"";width:68%;height:5px;background:#daf3a9;box-shadow:0 0 14px #a9ec6d;border-radius:6px;transform-origin:left;animation:trace .48s cubic-bezier(.22,1,.36,1) both}
.spark{position:absolute;width:76px;height:76px;top:315px;right:110px;animation:spark .86s cubic-bezier(.22,1,.36,1) both}
.captions{bottom:327px;left:86px;right:150px;text-align:center;line-height:1.12;font-size:48px;font-weight:760;overflow-wrap:anywhere}
.captions span{background:#16291fef;border-radius:16px;padding:6px 16px;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.captions.left{left:62px;right:auto;max-width:300px;text-align:left;bottom:405px}
.captions.right{left:auto;right:120px;max-width:405px;text-align:left;bottom:565px}
.captions.top{bottom:auto;top:315px;text-align:left}
.social{max-width:none;left:0;right:0;bottom:0;padding:185px 135px 195px 85px;overflow-wrap:anywhere;background:linear-gradient(transparent,#0d241adf 45%,#0c1b14);animation:arrive .4s cubic-bezier(.16,1,.3,1) both}
.content{display:flex;gap:24px;align-items:center}.content svg{flex:0 0 76px;width:76px;height:76px;background:white;padding:10px;border-radius:18px}
.brand{font-size:59px;font-weight:800;line-height:1.04;margin:0 0 24px}
.action{font-size:42px;font-weight:700;line-height:1.08}.address{font-size:55px;line-height:1.08;color:#e7f2d1;overflow-wrap:anywhere;margin-top:14px}
.address.long{font-size:46px}
.watermark{position:absolute;top:90px;left:60px;background:#111c;padding:12px 20px;font-size:37px;border:2px solid #fff}
.end{position:absolute;inset:0;background:#000;animation:finish var(--fade) linear both}
@keyframes arrive{from{opacity:0;transform:translateY(34px)}to{opacity:1;transform:translateY(0)}}
@keyframes trace{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes spark{0%{opacity:0;transform:scale(.2) rotate(-18deg)}48%{opacity:1;transform:scale(1.1) rotate(8deg)}100%{opacity:0;transform:scale(.85) translateY(-38px) rotate(24deg)}}
@keyframes finish{from{opacity:0}to{opacity:1}}
`;

export function hyperframesHtml(request: RenderRequest, withPicture: boolean): string {
  const {props, durationInFrames: frames} = request;
  if (!props.ad || !props.scenes || props.ad.engine !== 'hyperframes') {
    throw new WorkerError(422,'HYPERFRAMES_SOLICITUD_INVALIDA','HyperFrames requiere un anuncio con escenas.');
  }
  const {socialStart, fadeStart} = adTiming(frames);
  const {ad} = props;
  const overlays: string[] = [];
  let frame = 0;
  for (const scene of props.scenes) {
    const {label} = scene;
    if (label && frame + label.start_frame < socialStart) {
      const start = frame + label.start_frame;
      const end = Math.min(socialStart, frame + label.end_frame);
      overlays.push(`<div class="clip label ${frame === 0 ? 'first' : label.position}" ${timed(start,end,1)}>${escapeHtml(label.text)}</div>`);
      if (end - start >= 20) {
        const accentStart = start + 5;
        overlays.push(`<svg class="clip spark" aria-hidden="true" viewBox="0 0 48 48" ${timed(accentStart, Math.min(end, accentStart + 26), 2)}><path d="M24 2c0 14 8 22 22 22-14 0-22 8-22 22 0-14-8-22-22-22C16 24 24 16 24 2Z" fill="#e6ff9e"/></svg>`);
      }
    }
    frame += sceneLength(scene);
  }
  for (const cue of ad.captions) {
    const position = cue.end_frame > socialStart ? 'top' : cue.position ?? '';
    overlays.push(`<div class="clip captions ${position}" ${timed(cue.start_frame,cue.end_frame,3)}><span>${escapeHtml(cue.text)}</span></div>`);
  }
  const icon = ad.destination && socialIcons[ad.destination.channel];
  const address = ad.destination?.url.replace(/^https:\/\/(?:www\.)?/u,'');
  overlays.push(`<div class="clip social" ${timed(socialStart,frames,4)}>
      <p class="brand">${escapeHtml(ad.brand)}</p>
      <div class="content">${icon ? `<svg viewBox="0 0 24 24" role="img" aria-label="${icon.name}"><path fill="${icon.color}" d="${icon.path}"/></svg>` : ''}
        <div class="action">${escapeHtml(ad.cta)}</div>
      </div>${address ? `<div class="address ${address.length > 40 ? 'long' : ''}">${address.split('/').map(escapeHtml).join('/<wbr>')}</div>` : ''}</div>`);
  if (request.watermark) {
    overlays.push(`<div class="clip watermark" ${timed(0,frames,5)}>Creado con Agencia AI · Free</div>`);
  }
  overlays.push(`<div class="clip end" style="--fade:${Math.max(1, frames - 1 - fadeStart) / 30}s" ${timed(fadeStart,frames,6)}></div>`);
  const picture = withPicture ? `<video id="picture" class="clip" ${timed(0,frames,0)} src="./picture.mp4" muted playsinline></video>` : '';
  return `<!doctype html><html lang="es"><head><meta charset="utf-8"><style>${styles}</style></head><body>
<main id="main" data-no-timeline data-composition-id="main" data-start="0" data-width="${request.width}" data-height="${request.height}" data-duration="${frames / 30}" data-fps="30" style="width:${request.width}px;height:${request.height}px"><div id="layout" style="transform:scale(${request.width / 1080})">${picture}${overlays.join('')}</div></main></body></html>`;
}
