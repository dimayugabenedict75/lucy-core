/*
 * stelnet_audio.h — C ABI for embedding stelnet_audio in-process.
 *
 * This is a thin facade over engine::runtime. It adds no behaviour of its own:
 * every call maps onto ModelRegistry -> ILoadedVoiceModel -> IVoiceTaskSession,
 * the same surfaces stelnet_audio_cli uses.
 *
 * Contract:
 *   - Opaque handles only. No C++ type crosses this boundary.
 *   - No exception escapes. Every entry point returns stelnet_audio_status; the
 *     detail for the last failing call on THIS thread is stelnet_audio_last_error().
 *   - `const char *` and `const float *` outputs are BORROWED. They stay valid
 *     until the handle that produced them is freed or mutated. Copy anything
 *     you intend to keep.
 *   - Handles are not thread-safe individually. Separate handles may be used
 *     concurrently from separate threads.
 *   - Freeing NULL is a no-op, so cleanup paths need no null checks.
 *
 * Lifetime note: handles keep their parents alive internally (a session holds
 * its model, a model holds its registry). Freeing out of order is therefore
 * safe, which matters for garbage-collected callers where finalizer order is
 * not deterministic.
 */

#ifndef STELNET_AUDIO_H
#define STELNET_AUDIO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  if defined(STELNET_AUDIO_BUILD_DLL)
#    define STELNET_AUDIO_API __declspec(dllexport)
#  elif defined(STELNET_AUDIO_USE_DLL)
#    define STELNET_AUDIO_API __declspec(dllimport)
#  else
#    define STELNET_AUDIO_API
#  endif
#elif defined(__GNUC__)
#  define STELNET_AUDIO_API __attribute__((visibility("default")))
#else
#  define STELNET_AUDIO_API
#endif

/* ------------------------------------------------------------------ */
/* Versioning                                                          */
/* ------------------------------------------------------------------ */

#define STELNET_AUDIO_ABI_VERSION_MAJOR 0
#define STELNET_AUDIO_ABI_VERSION_MINOR 2
#define STELNET_AUDIO_ABI_VERSION_PATCH 0

/* Packed as (major << 16) | (minor << 8) | patch. A caller built against a
 * different MAJOR must not use the library. MINOR increments when entry points
 * are added -- nothing is removed or changed -- so a caller needing a newer one
 * can require a minimum; PATCH is behaviour only and must not be gated on. See
 * docs/c_api.md. */
STELNET_AUDIO_API uint32_t stelnet_audio_abi_version(void);

/* stelnet_audio's own build version, e.g. "0.2.1". Borrowed, static lifetime. */
STELNET_AUDIO_API const char * stelnet_audio_build_version(void);

/* ------------------------------------------------------------------ */
/* Status and errors                                                   */
/* ------------------------------------------------------------------ */

typedef enum stelnet_audio_status {
    STELNET_AUDIO_OK = 0,
    STELNET_AUDIO_ERR_INVALID_ARGUMENT = 1,
    STELNET_AUDIO_ERR_UNSUPPORTED_FAMILY = 2,
    STELNET_AUDIO_ERR_LOAD_FAILED = 3,
    STELNET_AUDIO_ERR_RUNTIME = 4,
    STELNET_AUDIO_ERR_OUT_OF_MEMORY = 5,
    STELNET_AUDIO_ERR_OUT_OF_RANGE = 6,
    STELNET_AUDIO_ERR_NOT_AVAILABLE = 7
} stelnet_audio_status;

/* Detail for the most recent failure on this thread. Read it immediately after
 * a call returns non-OK; a later successful call may clear it. Never NULL, and
 * borrowed until this thread's next call. */
STELNET_AUDIO_API const char * stelnet_audio_last_error(void);

/* Human-readable name for a status code. Borrowed, static lifetime. */
STELNET_AUDIO_API const char * stelnet_audio_status_string(stelnet_audio_status status);

/* ------------------------------------------------------------------ */
/* Handles                                                             */
/* ------------------------------------------------------------------ */

typedef struct stelnet_audio_registry stelnet_audio_registry;
typedef struct stelnet_audio_model    stelnet_audio_model;
typedef struct stelnet_audio_session  stelnet_audio_session;
typedef struct stelnet_audio_options  stelnet_audio_options;
typedef struct stelnet_audio_request  stelnet_audio_request;
typedef struct stelnet_audio_result   stelnet_audio_result;
typedef struct stelnet_audio_event    stelnet_audio_event;

/* ------------------------------------------------------------------ */
/* Options — mirrors the framework's string->string option maps 1:1.   */
/* ------------------------------------------------------------------ */

STELNET_AUDIO_API stelnet_audio_options * stelnet_audio_options_create(void);
STELNET_AUDIO_API stelnet_audio_status    stelnet_audio_options_set(stelnet_audio_options * options,
                                                     const char * key,
                                                     const char * value);
STELNET_AUDIO_API void               stelnet_audio_options_free(stelnet_audio_options * options);

/* ------------------------------------------------------------------ */
/* Registry                                                            */
/* ------------------------------------------------------------------ */

/* config_path may be NULL for the default loader set. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_registry_create(const char * config_path,
                                                      stelnet_audio_registry ** out_registry);
STELNET_AUDIO_API void            stelnet_audio_registry_free(stelnet_audio_registry * registry);

STELNET_AUDIO_API size_t          stelnet_audio_registry_family_count(const stelnet_audio_registry * registry);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_registry_family(const stelnet_audio_registry * registry,
                                                      size_t index,
                                                      const char ** out_family);

/* ---- Task vocabulary -------------------------------------------------------
 *
 * The tasks this build knows, askable without a model. Every other task-aware
 * entry point takes an stelnet_audio_model, so a caller that is deciding what to
 * install -- reading model_specs/*.json to build a picker, say -- has had
 * nothing to ask and has had to hardcode a copy of the table in
 * src/framework/model_spec/metadata.cpp.
 *
 * Model specs and this ABI use different spellings for the same kinds: a spec
 * says "music", "sfx", "edit" or "audio_generation" where this says "gen",
 * "clone" for "clon", "design" for "vdes", "speaker" for "spk". Nine of the
 * fourteen are identical, which is what makes comparing them directly appear
 * to work.
 */

/* How many task tokens this build accepts. */
STELNET_AUDIO_API size_t stelnet_audio_task_count(void);

/* The canonical token at `index`, or NULL when out of range. The returned
 * pointer is static and outlives any call. */
STELNET_AUDIO_API const char * stelnet_audio_task_name(size_t index);

/* The canonical token for a model-spec task name ("music" -> "gen"), or NULL
 * when the name names no task kind -- which is also how a caller detects a
 * spec declaring a task this build cannot serve. */
STELNET_AUDIO_API const char * stelnet_audio_task_from_spec_name(const char * spec_task);

/* ------------------------------------------------------------------ */
/* Model                                                               */
/* ------------------------------------------------------------------ */

/* Model selection. Every field may be NULL:
 *   family_hint          --family, or NULL to identify the model from its path
 *   config_id            --config, for packages that ship several configs
 *   weight_id            --weight, for packages that ship several weight sets
 *   model_spec_override  --model-spec-override */

typedef struct stelnet_audio_model_config {
    const char * family_hint;
    const char * config_id;
    const char * weight_id;
    const char * model_spec_override;
} stelnet_audio_model_config;

/* config and options may both be NULL. options corresponds to --load-option. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_model_load(stelnet_audio_registry * registry,
                                                  const char * model_path,
                                                  const stelnet_audio_model_config * config,
                                                  const stelnet_audio_options * options,
                                                  stelnet_audio_model ** out_model);
STELNET_AUDIO_API void            stelnet_audio_model_free(stelnet_audio_model * model);

STELNET_AUDIO_API const char * stelnet_audio_model_family(const stelnet_audio_model * model);
STELNET_AUDIO_API const char * stelnet_audio_model_description(const stelnet_audio_model * model);

/* Capability queries. `task` is one of the tokens stelnet_audio_task_name()
 * enumerates -- "vad", "asr", "diar", "sep", "gen", "tts", "clon", "vc",
 * "s2s", "align", "vdes", "spk", "svc", "midi" -- and `mode` is "offline" or
 * "streaming".
 *
 * Returns 1 when supported and 0 otherwise, which includes a task or mode this
 * build does not recognise: a caller cannot tell a misspelled question from a
 * negative answer. Validate against stelnet_audio_task_name() first if that
 * distinction matters.
 *
 * (This comment previously gave "diarization" and "alignment" as examples.
 * Neither has ever parsed, so a caller following it was told "no" for every
 * model that does diarize, with nothing to indicate the question was
 * malformed.) */
STELNET_AUDIO_API int stelnet_audio_model_supports(const stelnet_audio_model * model,
                                         const char * task,
                                         const char * mode);
STELNET_AUDIO_API int stelnet_audio_model_supports_timestamps(const stelnet_audio_model * model);
STELNET_AUDIO_API int stelnet_audio_model_supports_speaker_reference(const stelnet_audio_model * model);
STELNET_AUDIO_API int stelnet_audio_model_supports_style_condition(const stelnet_audio_model * model);

STELNET_AUDIO_API size_t          stelnet_audio_model_language_count(const stelnet_audio_model * model);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_model_language(const stelnet_audio_model * model,
                                                     size_t index,
                                                     const char ** out_language);

/* Which option map a CliOptionInfo belongs to. */
typedef enum stelnet_audio_option_scope {
    STELNET_AUDIO_OPTION_SCOPE_REQUEST = 0,
    STELNET_AUDIO_OPTION_SCOPE_SESSION = 1,
    STELNET_AUDIO_OPTION_SCOPE_LOAD    = 2
} stelnet_audio_option_scope;

/* Runtime introspection of a model's declared options, straight off
 * ModelInspection::cli. This is what lets a binding enumerate what a family
 * accepts instead of hardcoding per-family knowledge.
 *
 * Every out parameter is optional — pass NULL for anything you don't want.
 * Strings that the model did not declare come back as "" rather than NULL, so
 * callers never need a null check on them. out_required is 1 or 0. */
STELNET_AUDIO_API size_t          stelnet_audio_model_option_count(const stelnet_audio_model * model,
                                                         stelnet_audio_option_scope scope);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_model_option(const stelnet_audio_model * model,
                                                   stelnet_audio_option_scope scope,
                                                   size_t index,
                                                   const char ** out_name,
                                                   const char ** out_value_name,
                                                   const char ** out_description,
                                                   const char ** out_default_value,
                                                   const char ** out_min_value,
                                                   const char ** out_max_value,
                                                   int * out_required);

/* ------------------------------------------------------------------ */
/* Session                                                             */
/* ------------------------------------------------------------------ */

/* backend is "cpu", "cuda", "hip"/"rocm", "vulkan", "metal" or "best";
 * NULL means "cpu". device and threads mirror --device / --threads. */
typedef struct stelnet_audio_backend_config {
    const char * backend;
    int          device;
    int          threads;
} stelnet_audio_backend_config;

/* backend_config may be NULL for CPU / device 0 / 1 thread.
 * options may be NULL. Corresponds to --session-option. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_session_create(const stelnet_audio_model * model,
                                                      const char * task,
                                                      const char * mode,
                                                      const stelnet_audio_backend_config * backend_config,
                                                      const stelnet_audio_options * options,
                                                      stelnet_audio_session ** out_session);
STELNET_AUDIO_API void            stelnet_audio_session_free(stelnet_audio_session * session);

STELNET_AUDIO_API const char * stelnet_audio_session_family(const stelnet_audio_session * session);

/* Optional. stelnet_audio_session_run() and stelnet_audio_stream_start() always prepare
 * with the request they are about to run -- preparation carries the input
 * length, so a session prepared for one request must not run another. Call this
 * only to force allocation early, with a request representative of what will
 * follow; the run still prepares. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_session_prepare(stelnet_audio_session * session,
                                                      const stelnet_audio_request * request);

STELNET_AUDIO_API stelnet_audio_status stelnet_audio_session_run(stelnet_audio_session * session,
                                                  const stelnet_audio_request * request,
                                                  stelnet_audio_result ** out_result);

/* ------------------------------------------------------------------ */
/* Request                                                             */
/* ------------------------------------------------------------------ */

STELNET_AUDIO_API stelnet_audio_request * stelnet_audio_request_create(void);
STELNET_AUDIO_API void               stelnet_audio_request_free(stelnet_audio_request * request);

/* language may be NULL. A non-empty language is ALSO written to the request's
 * "language" option, because that is what stelnet_audio_cli's --language does and
 * some families read only the option. A later stelnet_audio_request_set_option with
 * the same key overrides it. */
/* Sets the request text, and -- when `language` is non-NULL and non-empty --
 * both the transcript language and options["language"], mirroring what
 * stelnet_audio_cli's --language does. Some families read only the option, so the
 * two travel together by default.
 *
 * That coupling cannot be undone through this call: pass NULL and use
 * stelnet_audio_request_set_text_language() below to set the transcript language
 * alone. Needed because "does this model declare a language option" and "does
 * this model need a transcript language" are different questions with
 * different answers -- parakeet_tdt validates its request options strictly and
 * refuses a language it does not declare, while qwen3_forced_aligner declares
 * no language option and requires the transcript language anyway. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_text(stelnet_audio_request * request,
                                                       const char * text,
                                                       const char * language);

/* Sets the transcript language without touching options["language"].
 *
 * The reference server needs exactly this and reaches around the ABI for it
 * (drop_unsupported_language_option in app/server/runtime.cpp erases the option
 * and keeps the transcript language). A C ABI client could not express that:
 * set_text is the only way to reach the transcript language and it writes the
 * option as a side effect, so the two arrived together or not at all. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_text_language(stelnet_audio_request * request,
                                                                const char * language);

/* Interleaved float PCM. Copied into the request, so `samples` need not
 * outlive the call. `frames` is per-channel. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_audio(stelnet_audio_request * request,
                                                        const float * samples,
                                                        size_t frames,
                                                        int sample_rate,
                                                        int channels);

/* Speaker reference for cloning/conversion families. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_voice_audio(stelnet_audio_request * request,
                                                              const float * samples,
                                                              size_t frames,
                                                              int sample_rate,
                                                              int channels);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_voice_id(stelnet_audio_request * request,
                                                           const char * cached_voice_id);

/* Style conditioning, for families that advertise it
 * (stelnet_audio_model_supports_style_condition). These mirror --style-language,
 * --emotion, --speaking-rate, --pitch-shift, --energy-scale and --style-tag.
 * Each is independent; set only the ones you need. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_style_language(stelnet_audio_request * request,
                                                                 const char * language);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_emotion(stelnet_audio_request * request,
                                                          const char * emotion);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_speaking_rate(stelnet_audio_request * request,
                                                                float speaking_rate);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_pitch_shift(stelnet_audio_request * request,
                                                              float pitch_shift);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_energy_scale(stelnet_audio_request * request,
                                                               float energy_scale);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_style_tag(stelnet_audio_request * request,
                                                            const char * key,
                                                            const char * value);

/* Artifacts: opaque payloads a family produces or consumes -- speaker
 * embeddings, acoustic tokens, MIDI, diarization state. Persisting one from a
 * result and feeding it back into a later request is how voice state is reused
 * without re-deriving it. */
typedef enum stelnet_audio_artifact_kind {
    STELNET_AUDIO_ARTIFACT_SPEAKER_EMBEDDING = 0,
    STELNET_AUDIO_ARTIFACT_STYLE_EMBEDDING = 1,
    STELNET_AUDIO_ARTIFACT_PROMPT_EMBEDDING = 2,
    STELNET_AUDIO_ARTIFACT_ACOUSTIC_TOKENS = 3,
    STELNET_AUDIO_ARTIFACT_MIDI = 4,
    STELNET_AUDIO_ARTIFACT_TRANSCRIPT_ALIGNMENT = 5,
    STELNET_AUDIO_ARTIFACT_DIARIZATION_STATE = 6,
    STELNET_AUDIO_ARTIFACT_VAD_STATE = 7,
    STELNET_AUDIO_ARTIFACT_CUSTOM = 8
} stelnet_audio_artifact_kind;

/* The payload is copied, so it need not outlive the call. out_index (optional)
 * receives the artifact's position, for use with set_artifact_meta. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_add_artifact(stelnet_audio_request * request,
                                                           stelnet_audio_artifact_kind kind,
                                                           const char * id,
                                                           const void * payload,
                                                           size_t payload_bytes,
                                                           size_t * out_index);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_artifact_meta(stelnet_audio_request * request,
                                                                size_t index,
                                                                const char * key,
                                                                const char * value);

/* Corresponds to --request-option. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_option(stelnet_audio_request * request,
                                                         const char * key,
                                                         const char * value);

/* Sets a list-valued request option -- the transport for the `*_list` option
 * types the model spec already declares. `values` is `count` UTF-8 strings,
 * copied into the request, so neither the array nor the strings need outlive
 * the call. A second call with the same key REPLACES the list rather than
 * appending, matching set_option's assignment semantics.
 *
 * List options live in their own map, so a key set here is not visible to a
 * family reading single-valued options and vice versa; a family declares which
 * one it wants by the type it puts in its spec. `count` may be 0, which sets an
 * empty list -- distinct from never setting the key at all. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_request_set_option_array(stelnet_audio_request * request,
                                                               const char * key,
                                                               const char * const * values,
                                                               size_t count);

/* ------------------------------------------------------------------ */
/* Result                                                              */
/* ------------------------------------------------------------------ */
/* Accessors rather than a struct copy, so TaskResult can gain fields without
 * breaking the ABI. Every out parameter is optional. */

STELNET_AUDIO_API void stelnet_audio_result_free(stelnet_audio_result * result);

/* STELNET_AUDIO_ERR_NOT_AVAILABLE when the task produced no audio. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_audio(const stelnet_audio_result * result,
                                                   const float ** out_samples,
                                                   size_t * out_frames,
                                                   int * out_sample_rate,
                                                   int * out_channels);

/* STELNET_AUDIO_ERR_NOT_AVAILABLE when the task produced no text. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_text(const stelnet_audio_result * result,
                                                  const char ** out_text,
                                                  const char ** out_language);

STELNET_AUDIO_API size_t          stelnet_audio_result_segment_count(const stelnet_audio_result * result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_segment(const stelnet_audio_result * result,
                                                     size_t index,
                                                     int64_t * out_start_sample,
                                                     int64_t * out_end_sample,
                                                     float * out_confidence,
                                                     const char ** out_text);

STELNET_AUDIO_API size_t          stelnet_audio_result_speaker_turn_count(const stelnet_audio_result * result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_speaker_turn(const stelnet_audio_result * result,
                                                          size_t index,
                                                          int64_t * out_start_sample,
                                                          int64_t * out_end_sample,
                                                          const char ** out_speaker_id,
                                                          float * out_confidence,
                                                          const char ** out_text);

STELNET_AUDIO_API size_t          stelnet_audio_result_word_count(const stelnet_audio_result * result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_word(const stelnet_audio_result * result,
                                                  size_t index,
                                                  const char ** out_word,
                                                  int64_t * out_start_sample,
                                                  int64_t * out_end_sample,
                                                  float * out_confidence);

/* Named audio outputs, for families that emit more than one stream
 * (source separation, multi-speaker TTS). */
STELNET_AUDIO_API size_t          stelnet_audio_result_named_audio_count(const stelnet_audio_result * result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_named_audio(const stelnet_audio_result * result,
                                                         size_t index,
                                                         const char ** out_id,
                                                         const float ** out_samples,
                                                         size_t * out_frames,
                                                         int * out_sample_rate,
                                                         int * out_channels);

/* Artifacts the task produced. TaskResult carries a single artifact_output and
 * a list of output_artifacts; both are presented here as one flat list, the
 * single one first when present. The payload is borrowed. */
STELNET_AUDIO_API size_t          stelnet_audio_result_artifact_count(const stelnet_audio_result * result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_artifact(const stelnet_audio_result * result,
                                                      size_t index,
                                                      stelnet_audio_artifact_kind * out_kind,
                                                      const char ** out_id,
                                                      const void ** out_payload,
                                                      size_t * out_payload_bytes);
STELNET_AUDIO_API size_t          stelnet_audio_result_artifact_meta_count(const stelnet_audio_result * result,
                                                                 size_t index);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_result_artifact_meta(const stelnet_audio_result * result,
                                                           size_t index,
                                                           size_t meta_index,
                                                           const char ** out_key,
                                                           const char ** out_value);

/* ------------------------------------------------------------------ */
/* Streaming                                                           */
/* ------------------------------------------------------------------ */
/* Pull-based, mirroring IStreamingVoiceTaskSession. No callback crosses the
 * FFI boundary. Requires a session created with mode "streaming". */

typedef enum stelnet_audio_stream_input_kind {
    STELNET_AUDIO_STREAM_INPUT_NONE = 0,
    STELNET_AUDIO_STREAM_INPUT_AUDIO_CHUNKS = 1
} stelnet_audio_stream_input_kind;

typedef enum stelnet_audio_stream_output_kind {
    STELNET_AUDIO_STREAM_OUTPUT_FINAL_RESULT = 0,
    STELNET_AUDIO_STREAM_OUTPUT_PULL_EVENTS = 1
} stelnet_audio_stream_output_kind;

/* The chunk size the family wants; push that many frames per call where you
 * can. Every out parameter is optional. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_policy(const stelnet_audio_session * session,
                                                    stelnet_audio_stream_input_kind * out_input,
                                                    stelnet_audio_stream_output_kind * out_output,
                                                    int64_t * out_preferred_chunk_samples,
                                                    double * out_preferred_chunk_seconds);

/* request may be NULL. Resets any stream already in flight. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_start(stelnet_audio_session * session,
                                                   const stelnet_audio_request * request);

/* Feeds one chunk and returns the event it produced. out_event may be NULL if
 * the caller only wants the final result. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_push(stelnet_audio_session * session,
                                                  const float * samples,
                                                  size_t frames,
                                                  int sample_rate,
                                                  int channels,
                                                  int64_t start_sample,
                                                  stelnet_audio_event ** out_event);

/* Drains events the family queued itself. Sets *out_event to NULL when the
 * queue is empty; that is STELNET_AUDIO_OK, not an error. */
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_next_event(stelnet_audio_session * session,
                                                        stelnet_audio_event ** out_event);

STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_finish(stelnet_audio_session * session,
                                                    stelnet_audio_result ** out_result);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_stream_reset(stelnet_audio_session * session);

STELNET_AUDIO_API void stelnet_audio_event_free(stelnet_audio_event * event);
STELNET_AUDIO_API int  stelnet_audio_event_is_final(const stelnet_audio_event * event);

/* An event carries the same shapes a result does, so it is read through the
 * result accessors. Borrowed — valid until the event is freed. */
STELNET_AUDIO_API const stelnet_audio_result * stelnet_audio_event_as_result(const stelnet_audio_event * event);

typedef enum stelnet_audio_voice_activity_kind {
    STELNET_AUDIO_VOICE_ACTIVITY_SPEECH_START = 0,
    STELNET_AUDIO_VOICE_ACTIVITY_SPEECH_END = 1,
    STELNET_AUDIO_VOICE_ACTIVITY_SPEECH_SEGMENT = 2
} stelnet_audio_voice_activity_kind;

STELNET_AUDIO_API size_t          stelnet_audio_event_voice_activity_count(const stelnet_audio_event * event);
STELNET_AUDIO_API stelnet_audio_status stelnet_audio_event_voice_activity(const stelnet_audio_event * event,
                                                           size_t index,
                                                           stelnet_audio_voice_activity_kind * out_kind,
                                                           int64_t * out_sample,
                                                           float * out_probability);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* STELNET_AUDIO_H */
