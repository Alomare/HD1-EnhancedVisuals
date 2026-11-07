// Ambient occlusion on all daylight: the lighting so far (sun and ambient, after global lighting and
// before point and spot lights) times the occlusion. In place of the game's luminance_adaption pixel
// shader, which nothing runs; the render state is an opaque write, as blend's.
//
// t0/s0 a copy of light_accumulation (full size, HDR); t1/s1 the blurred occlusion in x (half size,
// already raised to the strength by ssao.hlsl). The buffers luminance_adaption binds (global_viewport,
// c0) aren't needed.

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);
Texture2D __tex_input_texture1 : register(t1);
SamplerState __samp_input_texture1 : register(s1);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

float ao_at(int2 p, int2 size)
{
	return __tex_input_texture1.Load(int3(clamp(p, int2(0, 0), size - 1), 0)).x;
}

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	uint w, h, aw, ah;
	__tex_input_texture0.GetDimensions(w, h);
	__tex_input_texture1.GetDimensions(aw, ah);
	float4 lit = __tex_input_texture0.Load(int3(int2(input.uv * float2(w, h)), 0));

	// Bilinear by hand from the half-size occlusion, so nothing depends on the samplers' state.
	float2 p = input.uv * float2(aw, ah) - 0.5;
	int2 i = int2(floor(p));
	float2 f = p - floor(p);
	int2 size = int2(aw, ah);
	float ao = lerp(lerp(ao_at(i, size), ao_at(i + int2(1, 0), size), f.x),
		lerp(ao_at(i + int2(0, 1), size), ao_at(i + int2(1, 1), size), f.x), f.y);

	// Never true: keeps the samplers in the shader's tables, as luminance_adaption has them.
	if (lit.a < -1e30)
		lit += __tex_input_texture0.SampleLevel(__samp_input_texture0, input.uv, 0)
			+ __tex_input_texture1.SampleLevel(__samp_input_texture1, input.uv, 0);
	return float4(lit.rgb * ao, lit.a);
}
