// Ambient light, first step: a bright-weighted copy of the picture at a quarter of the width and height,
// in place of the game's filter:log_luminance pixel shader, which nothing runs. The render state is an
// opaque write, as copy's (the headers match apart from the name).
//
// t0/s0 the tone-mapped picture (albedo, full size). Writes a quarter-size HDR target that the game's own
// downsample and Gaussian filters then blur into a wide glow (tools/build.py, glow_chain).
//
// As ReShade's AmbientLight does, each pixel counts by the square of the brightest channel, so lit
// surfaces feed the glow and dark ones drop out, and the color is pushed away from grey, so the glow
// carries the light's tint. Unlike AmbientLight, every pixel of the 4x4 block counts, so small bright
// points don't flicker as the camera moves. GLOW_GAIN is set at build time, one shader per strength.

#ifndef GLOW_GAIN
#define GLOW_GAIN 0.45
#endif
#define SATURATION 1.6

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

float3 weighted(float3 c)
{
	c = saturate(c);
	float peak = max(c.r, max(c.g, c.b));
	float3 w = c * (peak * peak);
	float grey = (w.r + w.g + w.b) / 3.0;
	return max(lerp(float3(grey, grey, grey), w, SATURATION), 0.0);
}

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	uint w, h;
	__tex_input_texture0.GetDimensions(w, h);
	int2 size = int2(w, h);
	// The uv at the middle of this pixel lands in the middle of the 4x4 block it stands for.
	int2 first = int2(floor(input.uv * float2(w, h))) - 2;
	float3 sum = 0.0;
	[unroll] for (int y = 0; y < 4; y++)
	{
		[unroll] for (int x = 0; x < 4; x++)
		{
			int2 p = clamp(first + int2(x, y), int2(0, 0), size - 1);
			sum += weighted(__tex_input_texture0.Load(int3(p, 0)).rgb);
		}
	}
	float3 glow = sum * (GLOW_GAIN / 16.0);
	// Never true: keeps the sampler in the shader's tables, as the shader it replaces has it.
	if (glow.r < -1.0)
		glow += __tex_input_texture0.SampleLevel(__samp_input_texture0, input.uv, 0).rgb;
	return float4(glow, 0.0);
}
