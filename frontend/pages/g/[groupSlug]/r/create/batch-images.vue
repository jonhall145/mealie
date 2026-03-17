<template>
  <div>
    <div>
      <v-card-title class="headline">
        Bulk Recipe Import from Images
      </v-card-title>
      <v-card-text>
        <p>
          Upload up to 20 images — one recipe per image. They'll be processed as a batch
          via Claude API (Haiku) for fast, cheap extraction.
        </p>
      </v-card-text>

      <!-- Upload Section -->
      <div v-if="!batchId" class="px-4">
        <AppButtonUpload
          class="ml-auto"
          url="none"
          file-name="images"
          accept="image/*"
          :text="uploadedImages.length ? 'Upload More Images' : 'Upload Images'"
          :text-btn="false"
          :post="false"
          :multiple="true"
          @uploaded="uploadImages"
        />

        <div v-if="uploadedImages.length" class="mt-4">
          <p class="text-subtitle-1 mb-2">
            {{ uploadedImages.length }} image{{ uploadedImages.length > 1 ? 's' : '' }} selected
            (each will become a separate recipe)
          </p>
          <v-row>
            <v-col
              v-for="(imageUrl, index) in previewUrls"
              :key="index"
              cols="6"
              sm="4"
              md="3"
              lg="2"
            >
              <v-card variant="outlined" class="pa-1">
                <v-img
                  :src="imageUrl"
                  height="150"
                  cover
                  class="rounded"
                />
                <v-card-actions class="pa-0 pt-1 justify-center">
                  <v-btn
                    size="small"
                    color="error"
                    variant="text"
                    @click="removeImage(index)"
                  >
                    Remove
                  </v-btn>
                </v-card-actions>
              </v-card>
            </v-col>
          </v-row>
        </div>

        <v-card-actions v-if="uploadedImages.length" class="justify-center mt-4">
          <div style="width: 250px">
            <v-btn
              color="primary"
              block
              rounded
              size="large"
              :loading="submitting"
              :disabled="uploadedImages.length === 0"
              @click="submitBatch"
            >
              Process {{ uploadedImages.length }} Recipe{{ uploadedImages.length > 1 ? 's' : '' }}
            </v-btn>
          </div>
        </v-card-actions>
      </div>

      <!-- Progress Section -->
      <div v-if="batchId" class="px-4">
        <v-card variant="tonal" class="pa-4 mb-4">
          <div class="d-flex align-center mb-2">
            <v-progress-circular
              v-if="!batchDone"
              indeterminate
              size="24"
              width="3"
              color="primary"
              class="mr-3"
            />
            <v-icon v-else color="success" class="mr-3">
              mdi-check-circle
            </v-icon>
            <span class="text-h6">
              {{ batchDone ? 'Batch Complete!' : 'Processing...' }}
            </span>
          </div>

          <div v-if="batchStatus" class="mt-2">
            <v-row dense>
              <v-col cols="6" sm="3">
                <div class="text-caption text-medium-emphasis">
                  Processing
                </div>
                <div class="text-h6">
                  {{ batchStatus.request_counts.processing }}
                </div>
              </v-col>
              <v-col cols="6" sm="3">
                <div class="text-caption text-medium-emphasis">
                  Succeeded
                </div>
                <div class="text-h6 text-success">
                  {{ batchStatus.request_counts.succeeded }}
                </div>
              </v-col>
              <v-col cols="6" sm="3">
                <div class="text-caption text-medium-emphasis">
                  Errored
                </div>
                <div class="text-h6 text-error">
                  {{ batchStatus.request_counts.errored }}
                </div>
              </v-col>
              <v-col cols="6" sm="3">
                <div class="text-caption text-medium-emphasis">
                  Total
                </div>
                <div class="text-h6">
                  {{ totalRequests }}
                </div>
              </v-col>
            </v-row>

            <v-progress-linear
              :model-value="progressPercent"
              color="primary"
              height="8"
              rounded
              class="mt-3"
            />
          </div>
        </v-card>

        <!-- Collect Results -->
        <div v-if="batchDone && !collected" class="text-center mb-4">
          <v-btn
            color="success"
            size="large"
            rounded
            :loading="collecting"
            @click="collectResults"
          >
            Create Recipes from Results
          </v-btn>
        </div>

        <!-- Created Recipes -->
        <div v-if="createdRecipes.length > 0">
          <v-card-title class="px-0">
            Created {{ createdRecipes.length }} Recipe{{ createdRecipes.length > 1 ? 's' : '' }}
          </v-card-title>
          <v-list>
            <v-list-item
              v-for="slug in createdRecipes"
              :key="slug"
              :to="`/g/${groupSlug}/r/${slug}`"
            >
              <v-list-item-title>{{ slug }}</v-list-item-title>
              <template #append>
                <v-icon>mdi-open-in-new</v-icon>
              </template>
            </v-list-item>
          </v-list>
        </div>

        <!-- Start Over -->
        <div v-if="collected" class="text-center mt-4">
          <v-btn
            variant="outlined"
            @click="reset"
          >
            Import More Recipes
          </v-btn>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
import { useUserApi } from "~/composables/api";
import { alert } from "~/composables/use-toast";

export default defineNuxtComponent({
  setup() {
    const api = useUserApi();
    const route = useRoute();
    const groupSlug = computed(() => route.params.groupSlug || "");

    // Upload state
    const uploadedImages = ref<File[]>([]);
    const previewUrls = ref<string[]>([]);
    const submitting = ref(false);

    // Batch state
    const batchId = ref<string | null>(null);
    const reportId = ref<string | null>(null);
    const imageDir = ref<string | null>(null);
    const idToFilename = ref<Record<string, string>>({});
    const batchStatus = ref<{
      id: string;
      processing_status: string;
      request_counts: {
        processing: number;
        succeeded: number;
        errored: number;
        canceled: number;
        expired: number;
      };
    } | null>(null);
    const batchDone = ref(false);
    const collecting = ref(false);
    const collected = ref(false);
    const createdRecipes = ref<string[]>([]);
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    const totalRequests = computed(() => {
      if (!batchStatus.value) return 0;
      const c = batchStatus.value.request_counts;
      return c.processing + c.succeeded + c.errored + c.canceled + c.expired;
    });

    const progressPercent = computed(() => {
      if (!batchStatus.value || totalRequests.value === 0) return 0;
      const done = batchStatus.value.request_counts.succeeded
        + batchStatus.value.request_counts.errored
        + batchStatus.value.request_counts.canceled
        + batchStatus.value.request_counts.expired;
      return Math.round((done / totalRequests.value) * 100);
    });

    function uploadImages(files: File[]) {
      const remaining = 20 - uploadedImages.value.length;
      const toAdd = files.slice(0, remaining);
      uploadedImages.value = [...uploadedImages.value, ...toAdd];
      previewUrls.value = [
        ...previewUrls.value,
        ...toAdd.map(file => URL.createObjectURL(file)),
      ];
      if (files.length > remaining) {
        alert.warning(`Only ${remaining} more images allowed (max 20). Extra images were skipped.`);
      }
    }

    function removeImage(index: number) {
      URL.revokeObjectURL(previewUrls.value[index]);
      uploadedImages.value.splice(index, 1);
      previewUrls.value.splice(index, 1);
    }

    async function submitBatch() {
      if (uploadedImages.value.length === 0) return;
      submitting.value = true;

      const { data, error } = await api.recipes.createBatchFromImages(uploadedImages.value);
      if (error || !data) {
        alert.error("Failed to submit batch. Is your OpenAI/Anthropic API key configured?");
        submitting.value = false;
        return;
      }

      batchId.value = data.batch_id;
      reportId.value = data.report_id;
      imageDir.value = data.image_dir;
      idToFilename.value = data.id_to_filename;
      submitting.value = false;

      // Start polling
      startPolling();
    }

    function startPolling() {
      pollInterval = setInterval(async () => {
        if (!batchId.value) return;
        const { data } = await api.recipes.getBatchStatus(batchId.value);
        if (data) {
          batchStatus.value = data;
          if (data.processing_status === "ended") {
            batchDone.value = true;
            if (pollInterval) {
              clearInterval(pollInterval);
              pollInterval = null;
            }
          }
        }
      }, 5000);
    }

    async function collectResults() {
      if (!batchId.value || !reportId.value || !imageDir.value) return;
      collecting.value = true;

      const { data, error } = await api.recipes.collectBatchResults(
        batchId.value,
        reportId.value,
        imageDir.value,
        idToFilename.value,
      );

      if (error || !data) {
        alert.error("Failed to collect batch results");
        collecting.value = false;
        return;
      }

      createdRecipes.value = data.created_recipes;
      collected.value = true;
      collecting.value = false;
      alert.success(`Created ${data.count} recipe${data.count !== 1 ? "s" : ""}!`);
    }

    function reset() {
      // Clean up preview URLs
      previewUrls.value.forEach(url => URL.revokeObjectURL(url));
      uploadedImages.value = [];
      previewUrls.value = [];
      batchId.value = null;
      reportId.value = null;
      imageDir.value = null;
      idToFilename.value = {};
      batchStatus.value = null;
      batchDone.value = false;
      collecting.value = false;
      collected.value = false;
      createdRecipes.value = [];
    }

    onUnmounted(() => {
      if (pollInterval) clearInterval(pollInterval);
      previewUrls.value.forEach(url => URL.revokeObjectURL(url));
    });

    return {
      groupSlug,
      uploadedImages,
      previewUrls,
      submitting,
      batchId,
      batchStatus,
      batchDone,
      collecting,
      collected,
      createdRecipes,
      totalRequests,
      progressPercent,
      uploadImages,
      removeImage,
      submitBatch,
      collectResults,
      reset,
    };
  },
});
</script>
