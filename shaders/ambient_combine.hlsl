// Ambient light, last step, in place of the game's blend pixel shader. The game's blend is a plain add of
// two textures, used by the bloom pyramid to sum its levels and once more to add the bloom to the picture.
// This shader keeps the plain add, exactly, wherever the first texture is no larger than the second: the
// pyramid's own sums (coarser level first), and the picture's pass when the render config hands the
// bloom over first, which it does while Ambient light is off. Only the picture's pass with the picture
// first (albedo, then the quarter-size bloom and glow) gets the ambient light look:
//
// - the dark tones deepen: each pixel loses a share of brightness that grows with (1 - luma)^2, as the
//   adaptation of ReShade's AmbientLight does at a fixed scene brightness;
// - the bloom and the wide glow are screen-blended in rather than added, so they lift the surroundings
//   of lit areas and spare what is already bright.
//
// Binds what blend binds: t0/s0 and t1/s1, sampled as blend samples them. DARKEN is set at build time.

#ifndef DARKEN
#define DARKEN 0.25
#endif

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);
Texture2D __tex_input_texture1 : register(t1);
SamplerState __samp_input_texture1 : register(s1);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	float4 a = __tex_input_texture0.Sample(__samp_input_texture0, input.uv);
	float4 b = __tex_input_texture1.Sample(__samp_input_texture1, input.uv);
	uint w0, h0, w1, h1;
	__tex_input_texture0.GetDimensions(w0, h0);
	__tex_input_texture1.GetDimensions(w1, h1);
	[branch] if (w0 <= w1)
		return a + b;

	float3 base = saturate(a.rgb);
	float luma = dot(base, float3(0.299, 0.587, 0.114));
	base *= 1.0 - DARKEN * (1.0 - luma) * (1.0 - luma);
	float3 light = saturate(b.rgb);
	return float4(base + (1.0 - base) * light, a.a + b.a);
}
