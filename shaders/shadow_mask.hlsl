// Softer sun shadow edges, in place of the pixel shaders of global_shadow_mask and
// global_shadow_mask:cloud_shadows. Each entry holds three permutations, built from this one source with
// the defines the build picks from the bindings of the stage it replaces (tools/build.py):
//   STEREO  the stage reads nv_stereo_params (the NVIDIA 3D Vision eye offset)
//   SMOKE   the stage reads shadow_map_color (particle shadows, Smoke casts shadows)
//   CLOUDS  the stage reads cloud_shadow_map (moving cloud shadows)
// Everything but the filter is the game's own math, step for step: the depth bias, the world position
// rebuilt from the view ray, the move into the shadow map, the smoke and the clouds.
//
// The game's filter is a 2x2 loop of hardware comparison taps half a texel apart, a footprint of 3x3
// texels, so edges stay hard and stepped. This one spreads SOFT_TAPS x SOFT_TAPS comparison taps over a
// disc of SOFT_RADIUS metres on the ground, never less than 1.5 texels, so the softness looks the same at
// every shadow map size. The depth bias grows by the same radius, so the wider footprint doesn't shadow a
// surface onto itself.
//
// The render setting g_tessellation_detail reaches every shader through global_viewport and no game
// shader reads it. The mod sets it to 2 under the compare key, and at exactly 2 this shader runs the
// game's filter. Any other value, the game's 1 or nothing filled in at all, gives the soft filter.

#ifndef SOFT_RADIUS
#define SOFT_RADIUS 0.025
#endif
#ifndef SOFT_TAPS
#define SOFT_TAPS 4
#endif

#ifdef CLOUDS
#define SHADOW_T t2
#define SHADOW_S s2
#define EXTRA_T t3
#define EXTRA_S s3
#else
#define SHADOW_T t1
#define SHADOW_S s1
#define EXTRA_T t2
#define EXTRA_S s2
#endif

cbuffer global_viewport : register(b0)
{
	float3 camera_unprojection;
	float3 camera_pos;
	float4 camera_view[4];
	float4 camera_inv_view[4];
	float4 camera_inv_projection[4];
	float4 camera_world[4];
	float time;
	float delta_time;
	float2 back_buffer_size;
	float screen_aspect;
	float g_tessellation;
	float g_tessellation_tri_size;
	float g_tessellation_detail;
};

cbuffer c0 : register(b1)
{
	float4 world_view_proj[4];
	float4 world_to_shadow_map[4];
	float cloud_shadow_opacity;
	float2 cloud_shadow_scale;
	float2 cloud_shadow_movement;
};

Texture2D __tex_depth : register(t0);
SamplerState __samp_depth : register(s0);
#ifdef CLOUDS
Texture2D __tex_cloud_shadow_map : register(t1);
SamplerState __samp_cloud_shadow_map : register(s1);
#endif
Texture2D<float> shadow_map : register(SHADOW_T);
SamplerComparisonState shadow_map_sampler : register(SHADOW_S);
#ifdef STEREO
Texture2D nv_stereo_params : register(EXTRA_T);
#endif
#ifdef SMOKE
Texture2D __tex_shadow_map_color : register(EXTRA_T);
SamplerState __samp_shadow_map_color : register(EXTRA_S);
#endif

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
	float4 ray : TEXCOORD1;
};

// One tap: the share of light (1 lit, 0 shadowed) and, with SMOKE, the particles' opacity there.
void tap(float2 uv, float depth, inout float lit, inout float smoke)
{
	lit += shadow_map.SampleCmpLevelZero(shadow_map_sampler, uv, depth);
#ifdef SMOKE
	smoke += __tex_shadow_map_color.SampleLevel(__samp_shadow_map_color, uv, 0).x;
#endif
}

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	bool game = abs(g_tessellation_detail - 2.0) < 0.01;
	float z = __tex_depth.Sample(__samp_depth, input.uv).x;
	float bias = saturate(z * 0.02) * 0.195 + 0.005;
	if (!game)
		bias += SOFT_RADIUS;
	z -= bias;

	float3 origin = float3(camera_inv_view[0].w, camera_inv_view[1].w, camera_inv_view[2].w);
#ifdef STEREO
	float eye = nv_stereo_params.Load(int3(0, 0, 0)).y * camera_unprojection.x;
	float3 side = float3(camera_inv_view[0].x, camera_inv_view[1].x, camera_inv_view[2].x) * eye;
	float3 world = (input.ray.xyz - side) / input.ray.w * z + side + origin;
#else
	float3 world = input.ray.xyz / input.ray.w * z + origin;
#endif
	float4 wp = float4(world, 1.0);
	float3 sc = float3(dot(wp, world_to_shadow_map[0]), dot(wp, world_to_shadow_map[1]), dot(wp, world_to_shadow_map[2]));

	uint sw, sh;
	shadow_map.GetDimensions(sw, sh);
	float2 texel = 1.0 / float2(sw, sh);
	float lit = 0.0, smoke = 0.0, count;
	[branch] if (game)
	{
		[unroll] for (int gy = 0; gy < 2; gy++)
			[unroll] for (int gx = 0; gx < 2; gx++)
				tap(sc.xy + (float2(gx, gy) - 0.5) * texel, sc.z, lit, smoke);
		count = 4.0;
	}
	else
	{
		float2 radius = SOFT_RADIUS * float2(length(world_to_shadow_map[0].xyz), length(world_to_shadow_map[1].xyz));
		radius = max(radius, 1.5 * texel);
		[unroll] for (int y = 0; y < SOFT_TAPS; y++)
			[unroll] for (int x = 0; x < SOFT_TAPS; x++)
				tap(sc.xy + ((float2(x, y) + 0.5) / SOFT_TAPS * 2.0 - 1.0) * radius, sc.z, lit, smoke);
		count = SOFT_TAPS * SOFT_TAPS;
	}
#ifdef SMOKE
	float light = saturate((lit - smoke) / count);
#else
	float light = lit / count;
#endif
#ifdef CLOUDS
	float2 cloud_uv = (sc.xy - float2(world_to_shadow_map[0].w, world_to_shadow_map[1].w)) * cloud_shadow_scale
		+ time * cloud_shadow_movement;
	light *= 1.0 - __tex_cloud_shadow_map.Sample(__samp_cloud_shadow_map, cloud_uv).x * cloud_shadow_opacity;
#endif
	return float4(light, light, light, 1.0);
}
