export const INSTANCE_TYPES = [
  'p6-b200.48xlarge',
  'p6-b300.48xlarge',
  'p5.4xlarge',
  'p5.48xlarge',
  'p5e.48xlarge',
  'p5en.48xlarge',
  'p4d.24xlarge',
  'p4de.24xlarge',
  'trn1.32xlarge',
  'trn2.48xlarge',
  'trn2.3xlarge',
];

export const AWS_REGIONS = [
  'us-east-1',
  'us-east-2',
  'us-west-1',
  'us-west-2',
  'eu-north-1',
  'eu-west-2',
  'eu-west-3',
  'eu-south-2',
  'ap-northeast-1',
  'ap-northeast-2',
  'ap-south-1',
  'ap-south-2',
  'ap-southeast-2',
  'ap-southeast-3',
  'ap-southeast-4',
  'sa-east-1',
];

export const REGION_LABELS = {
  'us-east-1': 'N. Virginia',
  'us-east-2': 'Ohio',
  'us-west-1': 'N. California',
  'us-west-2': 'Oregon',
  'eu-north-1': 'Stockholm',
  'eu-west-2': 'London',
  'eu-west-3': 'Paris',
  'eu-south-2': 'Spain',
  'ap-northeast-1': 'Tokyo',
  'ap-northeast-2': 'Seoul',
  'ap-south-1': 'Mumbai',
  'ap-south-2': 'Hyderabad',
  'ap-southeast-2': 'Sydney',
  'ap-southeast-3': 'Jakarta',
  'ap-southeast-4': 'Melbourne',
  'sa-east-1': 'São Paulo',
};

export const TARGET_RESOURCES = {
  'training-job': 'Training Job',
  'hyperpod-cluster': 'HyperPod Cluster',
  endpoint: 'Endpoint (Inference)',
};

export const VALID_DURATIONS = [
  ...Array.from({ length: 14 }, (_, index) => index + 1),
  ...Array.from({ length: 24 }, (_, index) => 21 + index * 7),
];

export const MAX_CONCURRENT_AWS_CALLS = 8;
export const MAX_COMBINATIONS = AWS_REGIONS.length * INSTANCE_TYPES.length;
