import {describe, expect, it, vi} from 'vitest';
import {extractMetadata, extractPageText, extractReference, publicUrl} from '../cloudflare-saas/studio-reference-worker/src/index.js';

describe('referencias públicas de marca', () => {
  it('admite solamente URLs HTTPS sin credenciales ni parámetros secretos', () => {
    expect(publicUrl('https://www.example.cl/perfil?lang=es#fotos')?.toString()).toBe('https://www.example.cl/perfil?lang=es');
    for (const input of ['http://site.cl', 'https://127.0.0.1/', 'https://[::1]/', 'https://nas.local/',
      'https://user:password@site.cl/', 'https://site.cl:444/', 'https://site.cl/?access_token=secreto']) {
      expect(publicUrl(input)).toBeNull();
    }
  });
  it('obtiene título y descripción como texto limitado', () => {
    expect(extractMetadata('<title>Vivero &amp; plantas</title><meta content="Plantas para tu hogar" property="og:description">'))
      .toEqual({title:'Vivero & plantas',description:'Plantas para tu hogar'});
  });
  it('extrae texto de la página sin incluir scripts ni formularios', () => {
    expect(extractPageText('<body><nav>Menú</nav><h1>Plantas para tu hogar</h1><script>instrucción ajena</script><form>clave</form><p>Cultivadas en el sur.</p></body>'))
      .toBe('Plantas para tu hogar Cultivadas en el sur.');
  });
  it('revalida redirecciones antes de enviar otra solicitud', async () => {
    const send = vi.fn(async () => new Response(null, {status:302, headers:{location:'https://127.0.0.1/privado'}}));
    expect(await extractReference('https://site.cl/', send as typeof fetch)).toEqual({status:'unavailable'});
    expect(send).toHaveBeenCalledTimes(1);
  });
  it('indica que no está disponible si el perfil exige acceso o supera el límite', async () => {
    const denied = vi.fn(async () => new Response('', {status:403}));
    expect(await extractReference('https://site.cl/', denied as typeof fetch)).toEqual({status:'unavailable'});
    const large = vi.fn(async () => new Response('x'.repeat(262145), {headers:{'content-type':'text/html'}}));
    expect(await extractReference('https://site.cl/', large as typeof fetch)).toEqual({status:'unavailable'});
  });
  it('consulta únicamente el contenido HTML público', async () => {
    const send = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      expect(url).toBe('https://site.cl/');
      expect(init).toMatchObject({method:'GET',redirect:'manual'});
      return new Response('<title>Vivero</title><meta name="description" content="Plantas">',
        {headers:{'content-type':'text/html; charset=utf-8'}});
    });
    expect(await extractReference('https://site.cl/', send as typeof fetch))
      .toEqual({status:'available',title:'Vivero',description:'Plantas',excerpt:''});
    expect(send.mock.calls[0]?.[1]).toMatchObject({method:'GET',redirect:'manual'});
  });
});
